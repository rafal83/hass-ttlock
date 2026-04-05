# Configuration du Repository GitHub

Ce fichier documente la configuration requise pour le repository.

## Settings à appliquer manuellement sur GitHub

### 1. Activer les Issues
- Aller dans Settings → Features
- Cocher "Issues"
- Cette option est requise pour la validation HACS

### 2. Configurer les Topics
Topics requis pour HACS :
- `home-assistant` (requis)
- `smart-lock`
- `ttlock`
- `homeassistant`
- `hacs`

### 3. Via l'API GitHub (optionnel)

```bash
# Activer les issues
curl -X PATCH \
  -H "Authorization: token GITHUB_TOKEN" \
  -H "Accept: application/vnd.github.v3+json" \
  https://api.github.com/repos/rafal83/hass-ttlock \
  -d '{"has_issues": true, "has_projects": false, "has_wiki": false}'

# Ajouter les topics
curl -X PUT \
  -H "Authorization: token GITHUB_TOKEN" \
  -H "Accept: application/vnd.github.v3+json" \
  https://api.github.com/repos/rafal83/hass-ttlock/topics \
  -d '{"names": ["home-assistant", "smart-lock", "ttlock", "homeassistant", "hacs"]}'
```

## Vérification

Après configuration, relancer le workflow HACS validation.
