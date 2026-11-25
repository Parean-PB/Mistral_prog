"""Workflow tts-notes-deck : transforme les notes de presentation en fichiers MP3.

Version Drive (v4) : le worker met a jour des fichiers MP3 PRE-CREES dans
Drive par le proprietaire du dossier (via le connecteur Drive). Le fichier
reste la propriete du compte client, le contenu compte sur SON quota :
c'est la sequence qui fonctionne avec un compte de service (qui n'a aucun
quota Drive et ne peut ni creer du contenu ni le posseder).

Une activite de nettoyage supprime aussi, dans le dossier cible, les
anciens fichiers tts_* appartenant au compte de service (epaves des
essais precedents, 0 octet).

Entree : la voix (voice_id Voxtral), une liste de notes (id + texte deja
verbalise), drive_folder_id (dossier Drive cible) et owner_email
(proprietaire des fichiers pre-crees).
Sortie : par note {id, filename, drive_file_id, chars}, ou {id, mp3_base64,
chars} en mode retrocompatible (sans drive_folder_id).
"""

import logging
from typing import Any

from pydantic import BaseModel, Field

import mistralai.workflows as workflows

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "voxtral-mini-tts-3-0"
DEFAULT_VOICE = "voice_592"  # Marie (fr), voix Voxtral 3
MP3_MIME = "audio/mpeg"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"


class NoteIn(BaseModel):
    id: str = Field(description="Identifiant de la note (ex. slide-02)")
    text: str = Field(description="Texte de la note, pret a etre parle")


class TTSNotesInput(BaseModel):
    notes: list[NoteIn]
    voice_id: str = Field(default=DEFAULT_VOICE, description="Identifiant de la voix Voxtral")
    model: str = Field(default=DEFAULT_MODEL, description="Modele TTS")
    drive_folder_id: str | None = Field(default=None)
    owner_email: str | None = Field(
        default=None,
        description="Proprietaire Drive des fichiers MP3 pre-crees (le compte de service n'est qu'un editeur).",
    )


class AudioOut(BaseModel):
    id: str
    mp3_base64: str = ""
    filename: str = ""
    drive_file_id: str = ""
    chars: int


class CleanupOut(BaseModel):
    removed: int = 0


def _make_client():
    import os

    from mistralai.client import Mistral

    api_key = os.environ.get("MISTRAL_API_KEY")
    if api_key:
        return Mistral(api_key=api_key)
    return Mistral()  # le SDK lit le token de service-account du worker


def _google_credentials():
    """Charge les credentials Google du service account, ou None si absents."""
    import os
    import tempfile

    try:
        from google.oauth2 import service_account
    except ImportError:
        logger.warning("google-auth non installe : upload Drive impossible, retour base64")
        return None

    raw = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    if raw:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf8") as fh:
            fh.write(raw)
            path = fh.name
        try:
            return service_account.Credentials.from_service_account_file(path, scopes=[DRIVE_SCOPE])
        finally:
            os.unlink(path)

    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if path and os.path.exists(path):
        return service_account.Credentials.from_service_account_file(path, scopes=[DRIVE_SCOPE])
    return None


def _drive_filename(note_id: str) -> str:
    """Nom de fichier Drive sur, limite aux caracteres simples."""
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in note_id).strip("-")
    return f"tts_{safe or 'note'}.mp3"


def _list_tts_files(service: Any, folder_id: str):
    """Liste les fichiers tts_* du dossier avec leur proprietaire."""
    files = []
    token = None
    while True:
        res = (
            service.files()
            .list(
                q=f"parents='{folder_id}' and trashed=false",
                fields="nextPageToken, files(id, name, size, owners(emailAddress))",
                pageToken=token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        for f in res.get("files", []):
            if f.get("name", "").startswith("tts_"):
                files.append(f)
        token = res.get("nextPageToken")
        if not token:
            return files


@workflows.activity()
async def resolve_upload_mode(folder_id: str | None, owner_email: str | None) -> "tuple[str | None, str | None]":
    """Retourne (folder_id, owner_email) si l'upload est possible, sinon (None, None).

    Doit rester une ACTIVITE : elle lit l'environnement et importe la
    librairie Google, ce que le sandbox deterministe du workflow interdit.
    """
    if not folder_id or not owner_email:
        return None, None
    if _google_credentials() is None:
        logger.warning("credentials Google absents : sortie base64")
        return None, None
    return folder_id, owner_email


@workflows.activity()
async def cleanup_folder(folder_id: str, owner_email: str) -> CleanupOut:
    """Supprime du dossier les fichiers tts_* qui n'appartiennent PAS au
    proprietaire (epaves 0 octet creees par le compte de service)."""
    from googleapiclient.discovery import build

    creds = _google_credentials()
    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    removed = 0
    for f in _list_tts_files(service, folder_id):
        owners = f.get("owners", [])
        owner = owners[0].get("emailAddress", "") if owners else ""
        if owner and owner != owner_email:
            service.files().delete(fileId=f["id"], supportsAllDrives=True).execute()
            logger.info("Fichier epave supprime : %s (%s)", f["name"], owner)
            removed += 1
    return CleanupOut(removed=removed)


@workflows.activity()
async def synthesize_note(note: NoteIn, voice_id: str, model: str) -> AudioOut:
    """Synthetise une note en MP3 via l'API Voxtral TTS."""
    client = _make_client()
    response = client.audio.speech.complete(
        model=model,
        input=note.text,
        voice_id=voice_id,
        response_format="mp3",
    )
    audio_b64 = response.audio_data
    if not audio_b64:
        raise RuntimeError(f"Reponse audio vide pour la note {note.id}")
    return AudioOut(id=note.id, mp3_base64=audio_b64, chars=len(note.text))


@workflows.activity()
async def upload_note(audio: AudioOut, folder_id: str, owner_email: str) -> AudioOut:
    """Charge le MP3 dans le fichier pre-cree (proprietaire : owner_email)."""
    import base64

    from googleapiclient.discovery import build
    from googleapiclient.http import MediaInMemoryUpload

    creds = _google_credentials()
    if creds is None:
        raise RuntimeError("Credentials Google absents")
    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    filename = _drive_filename(audio.id)

    target = None
    for f in _list_tts_files(service, folder_id):
        if f.get("name") == filename:
            owners = f.get("owners", [])
            if owners and owners[0].get("emailAddress") == owner_email:
                target = f["id"]
                break
    if target is None:
        raise RuntimeError(
            f"Fichier pre-cree introuvable : {filename} (proprietaire {owner_email}) dans le dossier {folder_id}"
        )

    media = MediaInMemoryUpload(base64.b64decode(audio.mp3_base64), mimetype=MP3_MIME)
    service.files().update(fileId=target, media_body=media, supportsAllDrives=True).execute()
    logger.info("MP3 charge dans %s (drive_file_id=%s)", filename, target)
    return AudioOut(
        id=audio.id, filename=filename, drive_file_id=target, chars=audio.chars
    )


@workflows.workflow.define(
    name="tts-notes-deck",
    workflow_display_name="TTS Notes de Deck",
    workflow_description="Genere un fichier MP3 par note de presentation, avec la voix Voxtral choisie. Les MP3 sont charges dans des fichiers Drive pre-crees par le proprietaire du dossier.",
)
class TTSNotesWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, input: TTSNotesInput) -> "list[AudioOut]":
        target_folder, owner_email = await resolve_upload_mode(input.drive_folder_id, input.owner_email)

        if not target_folder:
            results: list[AudioOut] = []
            for note in input.notes:
                results.append(await synthesize_note(note, input.voice_id, input.model))
            return results

        await cleanup_folder(target_folder, owner_email)

        results: list[AudioOut] = []
        for note in input.notes:
            audio = await synthesize_note(note, input.voice_id, input.model)
            results.append(await upload_note(audio, target_folder, owner_email))
        return results
