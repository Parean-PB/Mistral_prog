"""Workflow tts-notes-deck : transforme les notes de presentation en fichiers MP3.

Version Drive : si l'entree contient un drive_folder_id et que le worker
dispose des credentials Google (GOOGLE_APPLICATION_CREDENTIALS_JSON ou
GOOGLE_APPLICATION_CREDENTIALS), chaque MP3 est televerse directement dans
ce dossier Drive et le resultat ne contient plus de base64 (leger).

Entree : la voix (voice_id Voxtral), une liste de notes (id + texte deja
verbalise), et optionnellement drive_folder_id (dossier Drive cible).
Sortie : par note, soit {id, filename, drive_file_id, chars} (mode Drive),
soit {id, mp3_base64, chars} (mode retrocompatible, sans drive_folder_id).

L'appel a l'API audio utilise la cle MISTRAL_API_KEY si elle est presentee
(secret workspace lie au deployment), sinon le token de service-account du
worker. Aucune cle n'est stockee dans le depot.
"""

import logging
from typing import Any

from pydantic import BaseModel, Field

import mistralai.workflows as workflows

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "voxtral-mini-tts-3-0"
DEFAULT_VOICE = "voice_592"  # Marie (fr), voix Voxtral 3
MP3_MIME = "audio/mpeg"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"


class NoteIn(BaseModel):
    id: str = Field(description="Identifiant de la note (ex. slide-02)")
    text: str = Field(description="Texte de la note, pret a etre parle")


class TTSNotesInput(BaseModel):
    notes: list[NoteIn]
    voice_id: str = Field(default=DEFAULT_VOICE, description="Identifiant de la voix Voxtral")
    model: str = Field(default=DEFAULT_MODEL, description="Modele TTS")
    drive_folder_id: str | None = Field(
        default=None,
        description="Identifiant du dossier Google Drive ou televerser les MP3. Si absent, la sortie reste en base64 (retrocompatible).",
    )


class AudioOut(BaseModel):
    id: str
    mp3_base64: str = ""
    filename: str = ""
    drive_file_id: str = ""
    chars: int


def _make_client():
    from mistralai.client import Mistral
    import os

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
            return service_account.Credentials.from_service_account_file(
                path, scopes=[DRIVE_SCOPE]
            )
        finally:
            os.unlink(path)

    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if path and os.path.exists(path):
        return service_account.Credentials.from_service_account_file(
            path, scopes=[DRIVE_SCOPE]
        )
    return None


def _drive_filename(note_id: str) -> str:
    """Nom de fichier Drive sur, limite aux caracteres simples."""
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in note_id).strip("-")
    return f"tts_{safe or 'note'}.mp3"


def _upload_to_drive(mp3_b64: str, filename: str, folder_id: str, creds: Any) -> str:
    """Televerse le MP3 dans le dossier Drive et retourne l'identifiant du fichier."""
    import base64

    from googleapiclient.discovery import build
    from googleapiclient.http import MediaInMemoryUpload

    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    media = MediaInMemoryUpload(base64.b64decode(mp3_b64), mimetype=MP3_MIME)
    file_metadata = {"name": filename, "parents": [folder_id]}
    created = (
        service.files()
        .create(body=file_metadata, media_body=media, fields="id", supportsAllDrives=True)
        .execute()
    )
    return created["id"]


@workflows.activity()
async def resolve_upload_mode(folder_id: str | None) -> str | None:
    """Retourne le dossier Drive si l'upload est possible, sinon None.

    Doit rester une ACTIVITE : elle lit l'environnement et importe la
    librairie Google, ce que le sandbox deterministe du workflow interdit.
    """
    if not folder_id:
        return None
    if _google_credentials() is None:
        logger.warning(
            "drive_folder_id fourni mais credentials Google absents : sortie base64"
        )
        return None
    return folder_id


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
async def upload_note(audio: AudioOut, folder_id: str) -> AudioOut:
    """Televerse le MP3 dans le dossier Drive et retourne la sortie legere."""
    creds = _google_credentials()
    if creds is None:
        raise RuntimeError("Credentials Google absents : impossible de televerser sur Drive")
    filename = _drive_filename(audio.id)
    drive_file_id = _upload_to_drive(audio.mp3_base64, filename, folder_id, creds)
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
        target_folder: str | None = await resolve_upload_mode(input.drive_folder_id)

        results: list[AudioOut] = []
        for note in input.notes:
            audio = await synthesize_note(note, input.voice_id, input.model)
            if target_folder:
                uploaded = await upload_note(audio, target_folder)
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
