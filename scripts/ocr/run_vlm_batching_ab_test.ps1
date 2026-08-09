param(
    [string]$Python = "python",
    [string]$CasesDir = "evals/vlm_financials/cases",
    [string]$OutputRoot = "logs",
    [string]$CompanyNumbers = "12255332,14550848,14745294,14871909"
)

$ErrorActionPreference = "Stop"
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$scenarios = @(
    @{ Name = "locator4-extractor2"; Config = "evals/vlm_financials/configs/openrouter-open-weight.yaml" },
    @{ Name = "locator1-extractor2"; Config = "evals/vlm_financials/configs/openrouter-open-weight-locator1-extractor2.yaml" },
    @{ Name = "locator4-extractor1"; Config = "evals/vlm_financials/configs/openrouter-open-weight-locator4-extractor1.yaml" }
)

foreach ($scenario in $scenarios) {
    $outputDir = Join-Path $OutputRoot "vlm-batching-ab-$timestamp-$($scenario.Name)"
    & $Python scripts/ocr/vlm_financial_eval.py run `
        --config $scenario.Config `
        --cases-dir $CasesDir `
        --company-numbers $CompanyNumbers `
        --output-dir $outputDir `
        --run-name "qwen35-9b-batching-ab-$($scenario.Name)"
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Scenario $($scenario.Name) finished with evaluation errors; continuing so its artifacts can be compared."
    }
}
