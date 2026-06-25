# ============================================
# Agnes AI 快捷工具 v3 — 虾包
# 与 GUI 版共享 ~/.agnes/config.json 配置
# 用法：
#   . .\agnes-tools.ps1           # 加载函数
#   agnes-image "描述词" [尺寸]    # 生成图片
#   agnes-video "描述词" [帧数]    # 生成视频
# ============================================

$BASE = "https://apihub.agnes-ai.com"

# ── 从共享配置文件读取 ──
$CONFIG_PATH = Join-Path $env:USERPROFILE ".agnes\config.json"
$API_KEY = ""
$OUT_DIR = Join-Path $env:USERPROFILE "Desktop\Agnes生成"

if (Test-Path $CONFIG_PATH) {
    try {
        $cfg = Get-Content $CONFIG_PATH -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($cfg.api_key -and $cfg.api_key -notmatch "^sk-(你的|占位)") {
            $API_KEY = $cfg.api_key
        }
        if ($cfg.output_dir) {
            $OUT_DIR = $cfg.output_dir
        }
    } catch {
        Write-Host "[Config] 配置文件格式错误，使用默认设置" -ForegroundColor Yellow
    }
}

if (-not $API_KEY) {
    Write-Host "[Config] ⚠️ 未配置 API Key" -ForegroundColor Yellow
    Write-Host "  方法1: 运行 python agnes-gui.py → 点「设置」填入 Key" -ForegroundColor Gray
    Write-Host "  方法2: 直接编辑 $CONFIG_PATH" -ForegroundColor Gray
    Write-Host "  注册获取: https://platform.agnes-ai.com" -ForegroundColor Gray
}

[System.IO.Directory]::CreateDirectory($OUT_DIR) | Out-Null

function _Require-Key {
    if (-not $API_KEY) {
        Write-Host "❌ 未配置 API Key，请先在 GUI 版设置或编辑 ~/.agnes/config.json" -ForegroundColor Red
        throw "API Key not configured"
    }
}

function _Save-Media {
    param([string]$Url, [string]$Ext)
    $ts = Get-Date -Format "yyyyMMdd-HHmmss"
    # Sanitize prompt to short filename prefix (take first prompt argument from caller scope)
    $prefix = "generated"
    $filename = "${prefix}_${ts}.${Ext}"
    $outPath = Join-Path $OUT_DIR $filename
    Write-Host "[Save] Downloading to $outPath ..." -ForegroundColor Gray
    curl.exe -s -L -o $outPath $Url
    Write-Host "[Save] -> $outPath" -ForegroundColor Green
    return $outPath
}

function _Call-AgnesJson {
    param([string]$Method, [string]$Url, [string]$Body)
    $tmp = [System.IO.Path]::GetTempFileName() + ".json"
    [System.IO.File]::WriteAllText($tmp, $Body, [System.Text.UTF8Encoding]::new($false))
    try {
        if ($Method -eq "GET") {
            $result = curl.exe -s $Url -H "Authorization: Bearer $API_KEY"
        } else {
            $result = curl.exe -s -X $Method $Url `
                -H "Authorization: Bearer $API_KEY" `
                -H "Content-Type: application/json" `
                -d "@$tmp" `
                --connect-timeout 10 --max-time 120
        }
        $result | ConvertFrom-Json
    }
    finally { Remove-Item $tmp -ErrorAction SilentlyContinue }
}

function _Fix-Frames {
    param([int]$N)
    if (($N - 1) % 8 -ne 0) {
        return [Math]::Max(81, ([Math]::Floor(($N - 1) / 8) * 8 + 1))
    }
    return $N
}

# ========== Picture ==========
function agnes-image {
    param(
        [Parameter(Mandatory=$true)] [string]$Prompt,
        [string]$Size = "1024x1024",
        [string]$Model = "agnes-image-2.1-flash"
    )
    _Require-Key

    Write-Host "[Image] Generating: $Prompt" -ForegroundColor Cyan

    $body = @{
        model      = $Model
        prompt     = $Prompt
        size       = $Size
        n          = 1
        extra_body = @{ response_format = "url" }
    } | ConvertTo-Json -Compress

    $r = _Call-AgnesJson -Method POST -Url "$BASE/v1/images/generations" -Body $body

    if (-not $r.data) {
        Write-Host "[Image] ❌ 响应异常: $($r | ConvertTo-Json -Compress)" -ForegroundColor Red
        return
    }

    $url = $r.data[0].url
    Write-Host "[Image] Done!" -ForegroundColor Green
    Write-Host $url -ForegroundColor Yellow

    $saved = _Save-Media -Url $url -Ext "png"
    Write-Host "[Image] Saved: $saved" -ForegroundColor Green
}

# ========== Video ==========
function agnes-video {
    param(
        [Parameter(Mandatory=$true)] [string]$Prompt,
        [int]$Duration = 0,
        [int]$Frames = 0,
        [int]$FPS = 24,
        [int]$Width = 1152,
        [int]$Height = 768
    )
    _Require-Key

    # 时长优先（秒），未指定则用默认 5s
    if ($Duration -gt 0) {
        $targetFrames = $Duration * $FPS
        $Frames = [Math]::Max(81, [Math]::Min(441, $targetFrames))
        $Frames = _Fix-Frames $Frames
    }
    if ($Frames -le 0) { $Frames = 121 }
    $Frames = _Fix-Frames $Frames

    Write-Host "[Video] Submitting: $Prompt ($Frames frames, $FPS fps, ${Width}x${Height})" -ForegroundColor Cyan

    $body = @{
        model      = "agnes-video-v2.0"
        prompt     = $Prompt
        num_frames = $Frames
        frame_rate = $FPS
        width      = $Width
        height     = $Height
    } | ConvertTo-Json -Compress

    $r = _Call-AgnesJson -Method POST -Url "$BASE/v1/videos" -Body $body
    $videoId = $r.video_id
    if (-not $videoId) {
        Write-Host "[Video] ❌ 未获取到 video_id" -ForegroundColor Red
        Write-Host "  响应: $($r | ConvertTo-Json -Compress)" -ForegroundColor Red
        return
    }

    $estimated = if ($r.seconds) { $r.seconds } else { "?" }
    Write-Host "[Video] 已提交 | ID: $videoId | 预计: $estimated 秒" -ForegroundColor Yellow

    # 轮询
    $maxWait = 600
    $waited = 0
    $pollUrl = "$BASE/agnesapi?video_id=$videoId&model_name=agnes-video-v2.0"

    do {
        Start-Sleep -Seconds 5
        $waited += 5
        $result = _Call-AgnesJson -Method GET -Url $pollUrl -Body ""
        $status = $result.status
        $progress = $result.progress
        Write-Host "[Video] 等待... ${waited}s | $status ($progress%)" -ForegroundColor Gray
    } while ($status -ne "completed" -and $status -ne "failed" -and $waited -lt $maxWait)

    if ($result.status -eq "completed") {
        $url = if ($result.video_url) {
            $result.video_url
        } elseif ($result.url) {
            $result.url
        } else {
            $result.remixed_from_video_id
        }
        Write-Host "[Video] Done!" -ForegroundColor Green
        Write-Host $url -ForegroundColor Yellow

        $saved = _Save-Media -Url $url -Ext "mp4"
        Write-Host "[Video] Saved: $saved" -ForegroundColor Green
    }
    elseif ($result.status -eq "failed") {
        Write-Host "[Video] ❌ 生成失败: $($result.error)" -ForegroundColor Red
    }
    else {
        Write-Host "[Video] ⚠️ 轮询超时 (${maxWait}s)，当前状态: $status" -ForegroundColor Yellow
        Write-Host "  手动查询: $pollUrl" -ForegroundColor Gray
    }
}

# ── 启动提示 ──

$keyStatus = if ($API_KEY) { "已配置 ($($API_KEY.Substring(0, [Math]::Min(15, $API_KEY.Length)))...)" } else { "❌ 未配置" }
Write-Host @"

═══════════════════════════════════════
  Agnes AI 快捷工具 v3   🦞
  输出目录: $OUT_DIR
  API Key:  $keyStatus
═══════════════════════════════════════
  图片: agnes-image "描述" [尺寸]
  视频: agnes-video "描述" [-Duration 秒]
═══════════════════════════════════════

"@ -ForegroundColor Green
