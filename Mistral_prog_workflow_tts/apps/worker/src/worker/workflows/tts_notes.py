"""Workflow tts-notes-deck : transforme les notes de presentation en fichiers MP3.

Version Drive : si l'entree contient un drive_folder_id et que le worker
dispose des credentials Google (GOOGLE_APPLICATION_CREDENTIALS_JSON ou
GOOGLE_APPLICATION_CREDENTIALS), chaque MP3 est televerse directement dans
ce dossier Drive et le resultat ne contient plus de base64 (leger).

Un compte de service Google n'a aucun quota Drive : le fichier est donc
cree vide, la propriete est transferee a owner_email (qui doit avoir acces
au dossier), PUIS le contenu audio est charge (quota du vrai proprietaire).

Entree : la voix (voice_id Voxtral), une liste de notes (id + texte deja
verbalise), drive_folder_id (dossier Drive cible) et owner_email
(proprietaire Drive final des MP3).
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
        description="Compte Drive a qui transferer la propriete des MP3 (un compte de service n'a aucun quota).",
    )


class AudioOut(BaseModel):
    id: str
    mp3_base64: str = ""
    filename: str = ""
    drive_file_id: str = ""
    chars: int


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


def _upload_to_drive(mp3_b64: str, filename: str, folder_id: str, owner_email: str | None, creds: Any) -> str:
    """Cree le MP3 dans le dossier Drive et retourne l'identifiant du fichier.

    Sequence imposee par l'absence de quota des comptes de service :
    1. creation d'un fichier vide (0 octet, aucun quota requis),
    2. partage en ecriture, puis transfert de propriete a owner_email,
    3. chargement du contenu audio (compte sur le quota du proprietaire).
    """
    import base64

    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaInMemoryUpload

    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    created = (
        service.files()
        .create(body={"name": filename, "parents": [folder_id]}, fields="id", supportsAllDrives=True)
        .execute()
    )
    file_id = created["id"]

    if owner_email:
        service.permissions().create(
            fileId=file_id,
            body={"type": "user", "role": "writer", "emailAddress": owner_email},
            supportsAllDrives=True,
        ).execute()
        try:
            service.permissions().create(
                fileId=file_id,
                body={"type": "user", "role": "owner", "emailAddress": owner_email},
                transferOwnership=True,
                supportsAllDrives=True,
            ).execute()
        except HttpError as err:
            logger.warning("Transfert de propriete impossible (%s) : le fichier reste au compte de service", err)

    media = MediaInMemoryUpload(base64.b64decode(mp3_b64), mimetype=MP3_MIME)
    service.files().update(fileId=file_id, media_body=media, supportsAllDrives=True).execute()
    return file_id


@workflows.activity()
async def resolve_upload_mode(folder_id: str | None, owner_email: str | None) -> "tuple[str | None, str | None]":
    """Retourne (folder_id, owner_email) si l'upload est possible, sinon (None, None).

    Doit rester une ACTIVITE : elle lit l'environnement et importe la
    librairie Google, ce que le sandbox deterministe du workflow interdit.
    """
    if not folder_id:
        return None, None
    if _google_credentials() is None:
        logger.warning("drive_folder_id fourni mais credentials Google absents : sortie base64")
        return None, None
    return folder_id, owner_email


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
async def upload_note(audio: AudioOut, folder_id: str, owner_email: str | None) -> AudioOut:
    """Televerse le MP3 dans le dossier Drive et retourne la sortie legere."""
    creds = _google_credentials()
    if creds is None:
        raise RuntimeError("Credentials Google absents : impossible de televerser sur Drive")
    filename = _drive_filename(audio.id)
    drive_file_id = _upload_to_drive(audio.mp3_base64, filename, folder_id, owner_email, creds)
    logger.info("MP3 televerse : %s (drive_file_id=%s)", filename, drive_file_id)
    return AudioOut(
        id=audio.id, filename=filename, drive_file_id=drive_file_id, chars=audio.chars
    )


@workflows.workflow.define(
    name="tts-notes-deck",
    workflow_display_name="TTS Notes de Deck",
    workflow_description="Genere un fichier MP3 par note de presentation, avec la voix Voxtral choisie. Televerse directement sur Google Drive quand drive_folder_id est fourni.",
)
class TTSNotesWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, input: TTSNotesInput) -> "list[AudioOut]":
        # Le mode Drive se resout via une ACTIVITE (imports/env interdits dans le workflow).
        target_folder, owner_email = await resolve_upload_mode(input.drive_folder_id, input.owner_email)

        results: list[AudioOut] = []
        for note in input.notes:
            audio = await synthesize_note(note, input.voice_id, input.model)
            if target_folder:
                uploaded = await upload_note(audio, target_folder, owner_email)
                # Ne pas renvoyer le base64 dans le resultat (economie de taille)
                results.append(
                    AudioOut(
                        id=uploaded.id,
                        filename=uploaded.filename,
                        drive_file_id=uploaded.drive_file_id,
                        chars=uploaded.chars,
                    )
                )
            else:
                # Base64 par defaut pour l'existant
                results.append(audio)
        return results
