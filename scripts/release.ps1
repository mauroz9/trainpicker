<#
.SYNOPSIS
    Construye la imagen Docker de TrainPicker, la taggea con un tag
    versionado (AAAA.MM.DD.N) y la publica en el GitLab Container Registry.

.DESCRIPTION
    Nunca reutiliza un tag ya publicado, así el tag anterior queda
    disponible para rollback.

    Requiere:
      - Estar autenticado contra el registry con un token con permiso de
        escritura (write_registry): docker login registry.gitlab.com
      - docker buildx (incluido en Docker Desktop) para poder consultar
        tags existentes sin descargarlos.

    Ver README.md, sección "Desplegar en producción (GitLab Container
    Registry)".

.EXAMPLE
    $env:GITLAB_REGISTRY_IMAGE = "registry.gitlab.com/tu-namespace/trainpicker"
    ./scripts/release.ps1
#>

$ErrorActionPreference = "Stop"

if (-not $env:GITLAB_REGISTRY_IMAGE) {
    Write-Error "Debes definir la variable de entorno GITLAB_REGISTRY_IMAGE (ej. registry.gitlab.com/tu-namespace/trainpicker)"
    exit 1
}
$RegistryImage = $env:GITLAB_REGISTRY_IMAGE

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Push-Location $RepoRoot
try {
    $GitStatus = git status --short
    if ($GitStatus) {
        Write-Error "Hay cambios sin commitear. Confirma o descarta los cambios antes de publicar un release."
        Write-Host $GitStatus
        exit 1
    }

    $DatePrefix = Get-Date -Format "yyyy.MM.dd"
    $N = 1
    while ($true) {
        $Tag = "$DatePrefix.$N"
        docker buildx imagetools inspect "${RegistryImage}:${Tag}" *> $null
        if ($LASTEXITCODE -ne 0) { break }
        $N++
    }

    Write-Host "Construyendo ${RegistryImage}:${Tag}..."
    docker build -t "${RegistryImage}:${Tag}" .
    if ($LASTEXITCODE -ne 0) { exit 1 }

    Write-Host "Publicando ${RegistryImage}:${Tag}..."
    docker push "${RegistryImage}:${Tag}"
    if ($LASTEXITCODE -ne 0) { exit 1 }

    Write-Host ""
    Write-Host "Release publicado: ${RegistryImage}:${Tag}"
    Write-Host ""
    Write-Host "En el servidor:"
    Write-Host "  1. Edita .env.prod y pon IMAGE_TAG=$Tag"
    Write-Host "  2. docker compose --env-file .env.prod -f compose.prod.yml pull"
    Write-Host "  3. docker compose --env-file .env.prod -f compose.prod.yml up -d"
    Write-Host ""
    Write-Host "Rollback: vuelve a poner el IMAGE_TAG anterior en .env.prod y repite"
    Write-Host "los pasos 2 y 3 (la imagen anterior sigue disponible en el registry)."
}
finally {
    Pop-Location
}
