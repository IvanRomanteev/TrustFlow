param(
    [Parameter(Mandatory = $true)][string]$Out,
    [string]$Voice = 'Microsoft David Desktop',
    [int]$Rate = 1
)
$ErrorActionPreference = 'Stop'
$timelinePath = Join-Path $PSScriptRoot 'timeline.json'
$timeline = [System.IO.File]::ReadAllText($timelinePath, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
if (Test-Path -LiteralPath $Out) { throw 'Output directory must be new; existing files are preserved.' }
$target = New-Item -ItemType Directory -Path $Out
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $synth.SelectVoice($Voice)
    $synth.Rate = $Rate
    $synth.Volume = 100
    $format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
        22050,
        [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
        [System.Speech.AudioFormat.AudioChannel]::Mono
    )
    foreach ($scene in $timeline.scenes) {
        $number = 0
        foreach ($text in $scene.narration) {
            $number += 1
            $file = Join-Path $target.FullName ('scene-{0:D2}-cue-{1:D2}.wav' -f [int]$scene.id, $number)
            $synth.SetOutputToWaveFile($file, $format)
            $synth.Speak([string]$text)
            $synth.SetOutputToNull()
        }
    }
    $voiceRecord = @{ voice = $Voice; rate = $Rate; engine = 'Windows System.Speech / local SAPI'; network_calls = 0 }
    $json = $voiceRecord | ConvertTo-Json
    [System.IO.File]::WriteAllText((Join-Path $target.FullName 'voice.json'), $json, (New-Object System.Text.UTF8Encoding($false)))
    Write-Output ('Local narration cues created: ' + $target.FullName)
}
finally { $synth.Dispose() }
