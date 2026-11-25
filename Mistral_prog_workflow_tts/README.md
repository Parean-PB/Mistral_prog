# Mistral_prog

Workflows Mistral pour l'infrastructure Vibe de Parean Biotechnologies.

## Workflow : `tts-notes-deck`

Transforme les notes de presentation d'un deck en fichiers MP3 via l'API
Voxtral TTS (`voxtral-mini-tts-3-0`).

**Entree** :

```json
{
  "voice_id": "voice_478",
  "drive_folder_id": "17Ox6kmD-G3r5wy0xoW9ZSY7i0pCaz1V4",
  "notes": [
    { "id": "slide-01", "text": "..." }
  ]
}
```

- `voice_id` : identifiant de la voix Voxtral (ex. voice_478, anglais).
- `drive_folder_id` (optionnel) : dossier Google Drive ou televerser chaque
  MP3 (`tts_<id>.mp3`). La sortie est alors legere :
  `[{ id, filename, drive_file_id, chars }]`.
- Sans `drive_folder_id`, comportement retrocompatible : la sortie contient
  un MP3 encode en base64 par note (volumineux, a eviter).

Les textes doivent etre prets a etre parles (chiffres en toutes lettres,
sigles epelles, pas de markdown ni de caracteres speciaux).

### Deploiement

- Dockerfile : `deploy/docker/Dockerfile.worker`
- Secret workspace `mistral_api_key` lie a la variable d'environnement
  `MISTRAL_API_KEY` au deploiement (jamais de cle dans le depot).
- Secret workspace `google_sa_json` lie a la variable d'environnement
  `GOOGLE_APPLICATION_CREDENTIALS_JSON` : contenu JSON complet d'un service
  account Google ayant acces en ecriture au dossier Drive cible. Le dossier
  Drive doit etre partage avec le champ `client_email` du service account
  (role Editeur).
