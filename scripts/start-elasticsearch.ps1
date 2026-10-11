param(
    [Parameter(Mandatory = $true)]
    [string]$ElasticHome
)
$ErrorActionPreference = 'Stop'
$elasticRoot = (Resolve-Path -LiteralPath $ElasticHome).Path
$launcher = Join-Path $elasticRoot 'bin\elasticsearch.bat'
if (-not (Test-Path -LiteralPath $launcher)) {
    throw "Elasticsearch launcher not found: $launcher"
}
$env:ES_JAVA_OPTS = '-Xms512m -Xmx512m'
Write-Host 'Starting local Elasticsearch at http://127.0.0.1:9200; Ctrl+C stops it.'
& $launcher '-Ediscovery.type=single-node' '-Expack.security.enabled=false' '-Enetwork.host=127.0.0.1' '-Ehttp.port=9200'
exit $LASTEXITCODE

