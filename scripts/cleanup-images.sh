#!/usr/bin/env bash
set -euo pipefail

# Limpia en el SERVIDOR las imágenes de TrainPicker descargadas del GitLab
# Container Registry que ya no corresponden al IMAGE_TAG desplegado
# actualmente. Cada release (scripts/release.sh) publica un tag nuevo y
# nunca borra los anteriores en el registry (para poder hacer rollback), así
# que en el servidor cada `pull` solo suma imágenes al disco si no se
# limpian las viejas.
#
# Nunca borra la imagen del IMAGE_TAG actual (ni, por tanto, ninguna imagen
# en uso por un contenedor en marcha).
#
# Uso (en el servidor, con .env.prod ya configurado):
#   GITLAB_REGISTRY_IMAGE=registry.gitlab.com/trainpicker-group/trainpicker-project \
#   IMAGE_TAG=2026.09.03.1 \
#   ./scripts/cleanup-images.sh
#
# También borra las imágenes "dangling" (<none>:<none>) que puedan quedar de
# builds locales (docker-compose.yml con build: .) sin tag asociado.

REGISTRY_IMAGE="${GITLAB_REGISTRY_IMAGE:?Debes exportar GITLAB_REGISTRY_IMAGE (el mismo valor que en .env.prod)}"
CURRENT_TAG="${IMAGE_TAG:?Debes exportar IMAGE_TAG con el tag actualmente desplegado (el mismo valor que en .env.prod)}"

echo "Imagen actual protegida: ${REGISTRY_IMAGE}:${CURRENT_TAG}"

OLD_TAGS="$(docker images "${REGISTRY_IMAGE}" --format '{{.Tag}}' | grep -v "^${CURRENT_TAG}\$" || true)"

if [[ -z "${OLD_TAGS}" ]]; then
  echo "No hay tags antiguos de ${REGISTRY_IMAGE} descargados en este host."
else
  echo "Borrando tags antiguos:"
  echo "${OLD_TAGS}" | sed 's/^/  - /'
  echo "${OLD_TAGS}" | while IFS= read -r tag; do
    docker rmi "${REGISTRY_IMAGE}:${tag}" || true
  done
fi

echo "Borrando imágenes dangling (<none>:<none>)..."
docker image prune -f
