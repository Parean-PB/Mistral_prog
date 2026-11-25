"""Workflow tts-notes-deck : transforme les notes de presentation en fichiers MP3.

Version Drive (v4) : le worker met a jour des fichiers MP3 PRE-CREES dans
Drive par le proprietaire du dossier (via le connecteur Drive). Le fichier
reste la propriete du compte client, le contenu compte sur SON quota :
c'est la sequence qui fonctionne avec un compte de service (qui n'a aucun
quota Drive et ne peut ni creer du contenu ni le posseder).

Version v5 : mode splice. Si l'entree contient html_file_id, le worker
telecharge le fichier HTML pre-uploade sur Drive, lit les MP3 tts_* du
dossier proprietaire, les concatene, et remplace le marqueur /__PA__/ par
la data URI audio en base64, puis met a jour le fichier Drive. Aucune
synthese TTS n'est relancee dans ce mode (cout nul).
"""

import io
import logging
from typing import Any

from googleapiclient.http import MediaIoBaseDownload
from pydantic import BaseModel, Field

import mistralai.workflows as workflows

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "voxtral-mini-tts-3-0"
DEFAULT_VOICE = "voice_592"  # Marie (fr), voix Voxtral 3
MP3_MIME = "audio/mpeg"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
HTML_MIME = "text/html"
AUDIO_MARKER = "/__PA__/"


class NoteIn(BaseModel):
    id: str = Field(description="Identifiant de la note (ex. slide-02)")
    text: str = Field(description="Texte de la note, pret a etre parle")


class TTSNotesInput(BaseModel):
    notes: list[NoteIn] = Field(default_factory=list)
    voice_id: str = Field(default=DEFAULT_VOICE, description="Identifiant de la voix Voxtral")
    model: str = Field(default=DEFAULT_MODEL, description="Modele TTS")
    drive_folder_id: str | None = Field(default=None)
    owner_email: str | None = Field(
        default=None,
        description="Proprietaire Drive des fichiers MP3 pre-crees (le compte de service n'est qu'un editeur).",
    )
    html_file_id: str | None = Field(
        default=None,
        description="Si fourni : mode splice, injection des MP3 tts_* du dossier dans ce fichier HTML Drive (marqueur /__PA__/). Aucune synthese relancee.",
    )


class AudioOut(BaseModel):
    id: str
    mp3_base64: str = ""
    filename: str = ""
    drive_file_id: str = ""
    chars: int


class CleanupOut(BaseModel):
    removed: int = 0


class SpliceOut(BaseModel):
    html_file_id: str
    slides: int
    audio_bytes: int
    html_chars: int


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


def _download_media(service: Any, file_id: str) -> bytes:
    """Telecharge le contenu binaire d'un fichier Drive."""
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _status, done = downloader.next_chunk()
    return buf.getvalue()


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
    return CleanupOut(removed)


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


@workflows.activity()
async def splice_audio(html_file_id: str, folder_id: str, owner_email: str) -> SpliceOut:
    """Injecte les MP3 tts_* du dossier dans le fichier HTML Drive.

    Telecharge le HTML, concatene les MP3 du proprietaire (tri par nom),
    encode en base64, remplace le marqueur AUDIO_MARKER, met a jour le
    fichier Drive. Tout reste dans cette activite : aucun base64 volumineux
    ne transite par l'historique du workflow.
    """
    import base64

    from googleapiclient.discovery import build
    from googleapiclient.http import MediaInMemoryUpload

    creds = _google_credentials()
    if creds is None:
        raise RuntimeError("Credentials Google absents")
    service = build("drive", "v3", credentials=creds, cache_discovery=False)

    html_bytes = _download_media(service, html_file_id)
    html = html_bytes.decode("utf-8")

    targets = []
    for f in _list_tts_files(service, folder_id):
        if not f.get("name", "").endswith(".mp3"):
            continue
        owners = f.get("owners", [])
        owner = owners[0].get("emailAddress", "") if owners else ""
        if owner == owner_email and int(f.get("size", "0")) > 0:
            targets.append((f["name"], f["id"], int(f.get("size", "0"))))
    targets.sort(key=lambda t: t[0])
    if len(targets) < 2:
        raise RuntimeError(f"Moins de 2 MP3 valides trouves dans {folder_id} pour {owner_email}")

    audio = b""
    for name, fid, size in targets:
        chunk = _download_media(service, fid)
        if len(chunk) != size:
            raise RuntimeError(f"Taille inattendue pour {name}: {len(chunk)} != {size}")
        audio += chunk
        logger.info("MP3 lu : %s (%d octets)", name, size)

    audio_b64 = base64.b64encode(audio).decode("ascii")
    data_uri = f"data:audio/mpeg;base64,{audio_b64}"
    if AUDIO_MARKER not in html:
        raise RuntimeError(f"Marqueur {AUDIO_MARKER} introuvable dans le HTML {html_file_id}")
    html_final = html.replace(AUDIO_MARKER, data_uri)
    if AUDIO_MARKER in html_final:
        raise RuntimeError("Marqueur toujours present apres remplacement")

    media = MediaInMemoryUpload(html_final.encode("utf-8"), mimetype=HTML_MIME)
    service.files().update(fileId=html_file_id, media_body=media, supportsAllDrives=True).execute()
    logger.info(
        "HTML mis a jour : %d MP3, %d octets audio, %d caracteres",
        len(targets), len(audio), len(html_final),
    )
    return SpliceOut(
        html_file_id=html_file_id,
        slides=len(targets),
        audio_bytes=len(audio),
        html_chars=len(html_final),
    )


@workflows.workflow.define(
    name="tts-notes-deck",
    workflow_display_name="TTS Notes de Deck",
    workflow_description="Genere un fichier MP3 par note de presentation, avec la voix Voxtral choisie. Les MP3 sont charges dans des fichiers Drive pre-crees par le proprietaire du dossier. Mode splice : injecte les MP3 existants dans un fichier HTML Drive.",
)
class TTSNotesWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, input: TTSNotesInput) -> "list[AudioOut]":
        # Mode splice : injection audio dans un HTML Drive, sans TTS.
        if input.html_file_id:
            target_folder, owner_email = await resolve_upload_mode(
                input.drive_folder_id, input.owner_email
            )
            if not target_folder or not owner_email:
                raise RuntimeError("drive_folder_id et owner_email requis pour le mode splice")
            spliced = await splice_audio(input.html_file_id, target_folder, owner_email)
            return [AudioOut(id="splice", filename=f"{spliced.slides} mp3", drive_file_id=spliced.html_file_id, chars=spliced.html_chars)]

        target_folder, owner_email = await resolve_upload_mode(input.drive_folder_id, input.owner_email)

        if not target_folder:
            results: list[AudioOut] = []
            for note in input.notes:
                results.append(await synthesize_note(note, input.voice_id, input.model))
            return results

        await cleanup_folder(target_folder, owner_email)

        results = []
        for note in input.notes:
            audio = await synthesize_note(note, input.voice_id, input.model)
            results.append(await upload_note(audio, target_folder, owner_email))
        return results
