# Mistral_prog

Workflows Mistral pour l'infrastructure Vibe de Parean Biotechnologies.

## Workflow : `tts-notes-deck`

Transforme les notes de presentation d'un deck en fichiers MP3 via l'API
Voxtral TTS (`voxtral-mini-tts-3-0`).

**Entree** :

```json
{
  "voice_id": "voice_592",
  "notes": [
    { "id": "slide-02", "text": "ImmuneWatch est un concurrent..." }
  ]
}
```

**Sortie** : une liste `{ id, mp3_base64, chars }`, un MP3 encode en base64 par note.

Les textes doivent etre prets a etre parles (chiffres en toutes lettres, sigles
epelles, pas de markdown ni de caracteres speciaux).

### Deploiement

- Dockerfile : `deploy/docker/Dockerfile.worker`
- Secret workspace `mistral_api_key` lie a la variable d'environnement
  `MISTRAL_API_KEY` au deploiement (jamais de cle dans le depot).
