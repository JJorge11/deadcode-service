<#
APAGA la plataforma en AWS: borra TODO lo que creó eks-up.ps1 (menos el
bucket del estado, que es permanente) y comprueba que no quede nada que
cobre. Uso:  .\scripts\eks-down.ps1   (o -Yes para no preguntar)
Los datos de RDS y de Prometheus se pierden: son de prueba.
#>
param([switch]$Yes)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
$Tf = 'infra/aws/platform'

Write-Host '==> terraform destroy (~5 min)' -ForegroundColor Cyan
# No hace falta borrar nada dentro del clúster antes: ni la app ni el
# monitoreo crean balanceadores ni discos en AWS (Postgres es RDS y
# Prometheus no tiene disco en EKS). Si un día los crean, borrarlos AQUÍ
# primero: Terraform no los conoce y quedarían huérfanos.
if ($Yes) { terraform "-chdir=$Tf" destroy -input=false -auto-approve } else { terraform "-chdir=$Tf" destroy -input=false }
if ($LASTEXITCODE -ne 0) { throw "terraform destroy falló (código $LASTEXITCODE): repetir el script" }

Write-Host "`n==> ¿Quedó algo que cobre? (todo debe ser 0)" -ForegroundColor Cyan
$p = '--profile', 'deadcode', '--output', 'text'
$checks = [ordered]@{
    'clústeres EKS' = { aws eks list-clusters @p --query 'length(clusters)' }
    'bases RDS'     = { aws rds describe-db-instances @p --query 'length(DBInstances)' }
    'NAT gateways'  = { aws ec2 describe-nat-gateways @p --filter 'Name=state,Values=pending,available,deleting' --query 'length(NatGateways)' }
    'IPs públicas'  = { aws ec2 describe-addresses @p --query 'length(Addresses)' }
    'máquinas EC2'  = { aws ec2 describe-instances @p --filters 'Name=instance-state-name,Values=pending,running,stopping,stopped' --query 'length(Reservations[].Instances[])' }
    'discos EBS'    = { aws ec2 describe-volumes @p --query 'length(Volumes)' }
    'balanceadores' = { aws elbv2 describe-load-balancers @p --query 'length(LoadBalancers)' }
    'snapshots RDS' = { aws rds describe-db-snapshots @p --query 'length(DBSnapshots)' }
}
$malos = 0
foreach ($k in $checks.Keys) {
    $n = "$(& $checks[$k])".Trim()
    if ($n -eq '0') { $color = 'Gray' } else { $malos++; $color = 'Red' }
    Write-Host ('  {0,-15} {1}' -f $k, $n) -ForegroundColor $color
}
Remove-Item (Join-Path $HOME '.kube\eks-deadcode') -ErrorAction SilentlyContinue
if ($malos) { throw "Quedaron $malos tipos de recursos vivos: revisar en la consola de AWS" }
Write-Host "`nAPAGADO: nada cobra (salvo el bucket del estado, ~0 USD)." -ForegroundColor Green
