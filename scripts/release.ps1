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
      - docker buildx (incluido en Docker Desktop). Se usa tanto para
        consultar tags existentes sin descargarlos como para hacer
        build+push en un único paso: con el containerd image store (por
        defecto en Docker Desktop reciente), separar "docker build" +
        "docker push" puede fallar con "blob unknown to registry" contra
        GitLab porque el content-store local no expone todos los blobs al
        hacer push después del build.

    La imagen se construye para $env:PLATFORM (por defecto linux/amd64, el
    estándar en VPS). Si tu máquina tiene otra arquitectura y no lo fijas
    explícitamente, buildx construiría por defecto para la arquitectura
    local y el servidor fallaría al hacer pull con "no matching manifest".
    Sobreescribe con PLATFORM=linux/arm64, o
    PLATFORM=linux/amd64,linux/arm64 para publicar ambas (más lento).

    Ver README.md, sección "Desplegar en producción (GitLab Container
    Registry)".

.EXAMPLE
    $env:GITLAB_REGISTRY_IMAGE = "registry.gitlab.com/trainpicker-group/trainpicker-project"
    ./scripts/release.ps1
#>

$ErrorActionPreference = "Stop"

if (-not $env:GITLAB_REGISTRY_IMAGE) {
    Write-Error "Debes definir la variable de entorno GITLAB_REGISTRY_IMAGE (ej. registry.gitlab.com/trainpicker-group/trainpicker-project)"
    exit 1
}
$RegistryImage = $env:GITLAB_REGISTRY_IMAGE
$Platform = if ($env:PLATFORM) { $env:PLATFORM } else { "linux/amd64" }

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

    Write-Host "Construyendo y publicando ${RegistryImage}:${Tag} (${Platform})..."
    docker buildx build --push --platform "${Platform}" -t "${RegistryImage}:${Tag}" .
    if ($LASTEXITCODE -ne 0) { exit 1 }

    Write-Host ""
    Write-Host "Release publicado: ${RegistryImage}:${Tag} (${Platform})"
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
