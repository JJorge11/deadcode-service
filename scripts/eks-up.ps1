<#
ENCIENDE la plataforma en AWS (etapa 7) y despliega la app con ArgoCD.
Uso (PowerShell, desde la carpeta del proyecto):
    aws login --profile deadcode      # si la sesión venció (dura 12 h)
    .\scripts\eks-up.ps1              # pide "yes" antes de crear en AWS
    .\scripts\eks-up.ps1 -Yes         # sin preguntar
Al terminar: .\scripts\eks-down.ps1 (¡cobra ~0,45 USD/h mientras esté encendido!).

Pasos (se puede repetir sin romper nada si el script se corta):
  1. Terraform: red + EKS + nodos + RDS (~11 min).
  2. kubeconfig APARTE (~/.kube/eks-deadcode): tu kubectl sigue en kind.
  3. ArgoCD (mismo manifiesto y versión que en kind).
  4. Secrets: Postgres (copiado de Secrets Manager, nunca se muestra) y
     Grafana (contraseña aleatoria nueva).
  5. Monitoreo PRIMERO: crea los tipos ServiceMonitor/PrometheusRule que
     usa nuestro chart (si la app fuera primero, su sync fallaría).
  6. La app (argocd/eks/deadcode.yaml: values.yaml + values-eks.yaml).
#>
param([switch]$Yes)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)

# Las herramientas instaladas con winget pueden no estar en el PATH de una
# terminal vieja: se recarga.
$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')

$Tf = 'infra/aws/platform'
$Kubeconfig = Join-Path $HOME '.kube\eks-deadcode'
$ArgoVersion = 'v3.6.1'

function Paso($texto) { Write-Host "`n==> $texto" -ForegroundColor Cyan }
# Los programas externos (aws, kubectl...) NO lanzan error en PowerShell 5.1
# cuando fallan: hay que mirar $LASTEXITCODE a mano.
function Revisar($que) { if ($LASTEXITCODE -ne 0) { throw "Falló: $que (código $LASTEXITCODE)" } }

Paso 'Sesión de AWS'
aws sts get-caller-identity --profile deadcode --query Arn --output text | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Sin sesión de AWS: ejecuta "aws login --profile deadcode"' }
Write-Host 'OK'

Paso '1/6 Terraform (red + EKS + RDS, ~11 min)'
if (-not (Test-Path "$Tf/.terraform")) {
    $bucket = terraform '-chdir=infra/aws/bootstrap' output -raw state_bucket; Revisar 'leer el bucket del estado'
    terraform "-chdir=$Tf" init -input=false "-backend-config=bucket=$bucket"; Revisar 'terraform init'
}
if ($Yes) { terraform "-chdir=$Tf" apply -input=false -auto-approve } else { terraform "-chdir=$Tf" apply -input=false }
Revisar 'terraform apply'

Paso '2/6 kubeconfig aparte'
aws eks update-kubeconfig --name deadcode --region us-east-2 --profile deadcode --kubeconfig $Kubeconfig --alias eks-deadcode | Out-Null
Revisar 'update-kubeconfig'
$env:KUBECONFIG = $Kubeconfig   # SOLO para este script
kubectl get nodes; Revisar 'kubectl get nodes'

Paso "3/6 ArgoCD $ArgoVersion"
kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f - | Out-Null
kubectl apply -n argocd --server-side --force-conflicts -f "https://raw.githubusercontent.com/argoproj/argo-cd/$ArgoVersion/manifests/install.yaml" | Out-Null
Revisar 'instalar ArgoCD'
kubectl rollout status deploy/argocd-repo-server -n argocd --timeout=300s; Revisar 'ArgoCD listo'

Paso '4/6 Secrets (sin mostrar contraseñas)'
foreach ($ns in 'deadcode', 'monitoring') { kubectl create namespace $ns --dry-run=client -o yaml | kubectl apply -f - | Out-Null }
# Postgres: AWS guardó usuario y contraseña en Secrets Manager. Se pasan a
# un Secret de Kubernetes con archivos temporales que se borran al instante
# (así la contraseña no aparece en pantalla ni en el historial).
$arn = terraform "-chdir=$Tf" output -raw db_secret_arn; Revisar 'leer db_secret_arn'
$dbName = terraform "-chdir=$Tf" output -raw db_name; Revisar 'leer db_name'
$cred = aws secretsmanager get-secret-value --profile deadcode --secret-id $arn --query SecretString --output text | ConvertFrom-Json
Revisar 'leer el secreto de RDS'
$tmp = Join-Path $env:TEMP ('deadcode-' + [guid]::NewGuid())
New-Item -ItemType Directory $tmp | Out-Null
try {
    $valores = @{ POSTGRES_USER = $cred.username; POSTGRES_PASSWORD = $cred.password; POSTGRES_DB = $dbName }
    foreach ($k in $valores.Keys) { [IO.File]::WriteAllText((Join-Path $tmp $k), $valores[$k]) }
    $desde = $valores.Keys | ForEach-Object { "--from-file=$_=$(Join-Path $tmp $_)" }
    kubectl create secret generic postgres-credentials -n deadcode @desde --dry-run=client -o yaml | kubectl apply -f - | Out-Null
    Revisar 'Secret de Postgres'
} finally {
    Remove-Item -Recurse -Force $tmp
    $cred = $null
}
Write-Host 'postgres-credentials: OK'
# Grafana: contraseña aleatoria NUEVA en cada clúster (como en kind).
$existe = kubectl get secret grafana-admin -n monitoring --ignore-not-found -o name
if (-not $existe) {
    $pw = -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 24 | ForEach-Object { [char]$_ })
    kubectl create secret generic grafana-admin -n monitoring --from-literal=admin-user=admin "--from-literal=admin-password=$pw" | Out-Null
    Revisar 'Secret de Grafana'
    $pw = $null
}
Write-Host 'grafana-admin: OK'

Paso '5/6 Monitoreo (Prometheus + Grafana)'
kubectl apply -f argocd/eks/monitoring.yaml; Revisar 'Application monitoring'
Write-Host 'Esperando los tipos ServiceMonitor y PrometheusRule...'
$hasta = (Get-Date).AddMinutes(8)
do {
    Start-Sleep 10
    $crds = kubectl get crd servicemonitors.monitoring.coreos.com prometheusrules.monitoring.coreos.com --ignore-not-found -o name
} while (@($crds).Count -lt 2 -and (Get-Date) -lt $hasta)
if (@($crds).Count -lt 2) { throw 'El monitoreo no creó sus tipos (CRDs) a tiempo: ver kubectl get application monitoring -n argocd' }

Paso '6/6 La app (ArgoCD despliega charts/deadcode-service con values-eks.yaml)'
kubectl apply -f argocd/eks/deadcode.yaml; Revisar 'Application deadcode'
$hasta = (Get-Date).AddMinutes(10)
do {
    Start-Sleep 10
    $estado = kubectl get application deadcode -n argocd -o jsonpath='{.status.sync.status} {.status.health.status}'
    Write-Host "  deadcode: $estado"
} while ($estado -ne 'Synced Healthy' -and (Get-Date) -lt $hasta)
if ($estado -ne 'Synced Healthy') { throw "La app no quedó sana: $estado (ver: kubectl get pods -n deadcode)" }

Write-Host "`nLISTO. La plataforma está ENCENDIDA (cobra). Para usarla, en otra terminal:" -ForegroundColor Green
Write-Host '  $env:KUBECONFIG = "$HOME\.kube\eks-deadcode"'
Write-Host '  kubectl port-forward -n deadcode svc/api 8090:8000      ->  http://127.0.0.1:8090/'
Write-Host '  (8090, no 8080: el 8080 es del clúster kind)'
Write-Host 'Al terminar: .\scripts\eks-down.ps1' -ForegroundColor Yellow
