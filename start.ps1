# Navigate to script directory
Set-Location -Path $PSScriptRoot

Write-Host "[*] Starting OutboundAI Mass Caller..."

# Load .env file
if (Test-Path ".env") {
    Get-Content ".env" | ForEach-Object {
        $line = $_.Trim()
        if ($line -match "^\s*#" -or $line -eq "") { return }
        
        $key, $value = $line -split "=", 2
        if ($key -and $value) {
            $key = $key.Trim()
            $value = $value.Trim()
            [System.Environment]::SetEnvironmentVariable($key, $value)
            Set-Item -Path "Env:\$key" -Value $value
        }
    }
}

Write-Host "[+] Configuration:"
Write-Host "   LiveKit: $env:LIVEKIT_URL"
Write-Host "   LLM:     $env:LLM_PROVIDER"
Write-Host "   DB:      $env:DB_HOST`:$env:DB_PORT/$env:DB_NAME"

Write-Host "[*] Starting FastAPI server on port 8000..."

# Start FastAPI in background
$serverProcess = Start-Process uvicorn -ArgumentList "server:app --host 0.0.0.0 --port 8000" -PassThru

Start-Sleep -Seconds 3

Write-Host "[*] Starting LiveKit agent worker..."
python agent.py start

# Stop server after agent exits
if ($serverProcess -ne $null) {
    Stop-Process -Id $serverProcess.Id -ErrorAction SilentlyContinue
}
