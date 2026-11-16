"""Workflow tts-notes-deck : transforme les notes de presentation en fichiers MP3.

Entree : la voix (voice_id Voxtral), et une liste de notes (id + texte deja
verbalise : chiffres en toutes lettres, sigles epelles, pas de markdown).
Sortie : un MP3 (base64) par note.

L'appel a l'API audio utilise la cle MISTRAL_API_KEY si elle est presentee
(secret workspace lie au deployment), sinon le token de service-account du
worker. Aucune cle n'est stockee dans le depot.
"""

import os

from pydantic import BaseModel, Field

import mistralai.workflows as workflows

DEFAULT_MODEL = "voxtral-mini-tts-3-0"
DEFAULT_VOICE = "voice_592"  # Marie (fr), voix Voxtral 3


class NoteIn(BaseModel):
    id: str = Field(description="Identifiant de la note (ex. slide-02)")
    text: str = Field(description="Texte de la note, pret a etre parle")


class TTSNotesInput(BaseModel):
    notes: list[NoteIn]
    voice_id: str = Field(default=DEFAULT_VOICE, description="Identifiant de la voix Voxtral")
    model: str = Field(default=DEFAULT_MODEL, description="Modele TTS")


class AudioOut(BaseModel):
    id: str
    mp3_base64: str
    chars: int


def _make_client():
    from mistralai.client import Mistral

    api_key = os.environ.get("MISTRAL_API_KEY")
    if api_key:
        return Mistral(api_key=api_key)
    return Mistral()  # le SDK lit le token de service-account du worker


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


@workflows.workflow.define(
    name="tts-notes-deck",
    workflow_display_name="TTS Notes de Deck",
    workflow_description="Genere un fichier MP3 (base64) par note de presentation, avec la voix Voxtral choisie.",
)
class TTSNotesWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, input: TTSNotesInput) -> list[AudioOut]:
        results: list[AudioOut] = []
        for note in input.notes:
            results.append(await synthesize_note(note, input.voice_id, input.model))
        return results
