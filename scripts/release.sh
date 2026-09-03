#!/usr/bin/env bash
set -euo pipefail

# Construye la imagen Docker de TrainPicker, la taggea con un tag versionado
# (AAAA.MM.DD.N) y la publica en el GitLab Container Registry. Nunca reutiliza
# un tag ya publicado, así el tag anterior queda disponible para rollback.
#
# Uso:
#   GITLAB_REGISTRY_IMAGE=registry.gitlab.com/trainpicker-group/trainpicker-project ./scripts/release.sh
#
# Requiere:
#   - Estar autenticado contra el registry con un token con permiso de
#     escritura (write_registry): docker login registry.gitlab.com
#   - docker buildx (incluido en Docker Desktop y en Docker Engine >= 19.03
#     con el plugin buildx instalado). Se usa tanto para consultar tags
#     existentes sin descargarlos como para hacer build+push en un único
#     paso: con el containerd image store (por defecto en Docker Desktop
#     reciente), separar `docker build` + `docker push` puede fallar con
#     "blob unknown to registry" contra GitLab porque el content-store local
#     no expone todos los blobs al hacer push después del build.
#
# Ver README.md, sección "Desplegar en producción (GitLab Container Registry)".

REGISTRY_IMAGE="${GITLAB_REGISTRY_IMAGE:?Debes exportar GITLAB_REGISTRY_IMAGE (ej. registry.gitlab.com/trainpicker-group/trainpicker-project)}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ -n "$(git status --short)" ]]; then
  echo "Hay cambios sin commitear. Confirma o descarta los cambios antes de publicar un release:" >&2
  git status --short >&2
  exit 1
fi

DATE_PREFIX="$(date +%Y.%m.%d)"
N=1
while docker buildx imagetools inspect "${REGISTRY_IMAGE}:${DATE_PREFIX}.${N}" >/dev/null 2>&1; do
  N=$((N + 1))
done
TAG="${DATE_PREFIX}.${N}"

echo "Construyendo y publicando ${REGISTRY_IMAGE}:${TAG}..."
docker buildx build --push -t "${REGISTRY_IMAGE}:${TAG}" .

cat <<EOF

Release publicado: ${REGISTRY_IMAGE}:${TAG}

En el servidor:
  1. Edita .env.prod y pon IMAGE_TAG=${TAG}
  2. docker compose --env-file .env.prod -f compose.prod.yml pull
  3. docker compose --env-file .env.prod -f compose.prod.yml up -d

Rollback: vuelve a poner el IMAGE_TAG anterior en .env.prod y repite los
pasos 2 y 3 (la imagen anterior sigue disponible en el registry).
EOF
