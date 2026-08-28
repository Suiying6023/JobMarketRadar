# Clash Verge Rev 外部控制器辅助（走命名管道 \\.\pipe\verge-mihomo，无 TCP API）。
# 用法:
#   powershell -File clash_pipe.ps1 list                        # 列出 Selector 代理组和当前节点
#   powershell -File clash_pipe.ps1 groups                      # 只列组名
#   powershell -File clash_pipe.ps1 nodes -Group <组名>          # 列出某组节点
#   powershell -File clash_pipe.ps1 switch -Group <组名> -Node <节点名>
#   powershell -File clash_pipe.ps1 rotate -Group <组名>         # 切到该组下一个不同节点，输出新节点
param(
    [string]$Action = 'list',
    [string]$Group = '',
    [string]$Node = ''
)
$ErrorActionPreference = 'Stop'
$AUTH = 'Bearer set-your-secret'

function Invoke-Api {
    param([string]$Method, [string]$Path, [string]$Body = '')
    $pipe = New-Object System.IO.Pipes.NamedPipeClientStream('.', 'verge-mihomo', [System.IO.Pipes.PipeDirection]::InOut, [System.IO.Pipes.PipeOptions]::None)
    $pipe.Connect(5000)
    $req = "$Method $Path HTTP/1.0`r`nHost: x`r`nAuthorization: $AUTH`r`n"
    if ($Body -ne '') {
        $req += "Content-Type: application/json`r`nContent-Length: $([Text.Encoding]::UTF8.GetByteCount($Body))`r`n"
    }
    $req += "`r`n"
    $reqBytes = [Text.Encoding]::UTF8.GetBytes($req + $Body)
    $pipe.Write($reqBytes, 0, $reqBytes.Length)
    $pipe.Flush()
    $ms = New-Object System.IO.MemoryStream
    $buf = New-Object byte[] 65536
    while ($true) {
        try { $n = $pipe.Read($buf, 0, $buf.Length) } catch { break }
        if ($n -le 0) { break }
        $ms.Write($buf, 0, $n)
    }
    $pipe.Dispose()
    $all = $ms.ToArray()
    if ($all.Length -eq 0) { return '' }
    # 找 header/body 分界
    $headerEnd = -1
    for ($i = 0; $i -lt $all.Length - 3; $i++) {
        if ($all[$i] -eq 13 -and $all[$i+1] -eq 10 -and $all[$i+2] -eq 13 -and $all[$i+3] -eq 10) {
            $headerEnd = $i + 4; break
        }
    }
    if ($headerEnd -lt 0) { return [Text.Encoding]::UTF8.GetString($all) }
    $headerStr = [Text.Encoding]::ASCII.GetString($all, 0, $headerEnd)
    $bodyLen = $all.Length - $headerEnd
    $bodyBytes = New-Object byte[] $bodyLen
    [Array]::Copy($all, $headerEnd, $bodyBytes, 0, $bodyLen)
    if ($headerStr -match 'chunked') {
        # 字节级 de-chunk
        $out = New-Object System.IO.MemoryStream
        $pos = 0
        while ($pos -lt $bodyBytes.Length - 1) {
            $crlf = -1
            for ($j = $pos; $j -lt $bodyBytes.Length - 1; $j++) {
                if ($bodyBytes[$j] -eq 13 -and $bodyBytes[$j+1] -eq 10) { $crlf = $j; break }
            }
            if ($crlf -lt 0) { break }
            $sizeHex = [Text.Encoding]::ASCII.GetString($bodyBytes, $pos, $crlf - $pos).Trim()
            if ($sizeHex -eq '') { $pos = $crlf + 2; continue }
            $size = [Convert]::ToInt32($sizeHex, 16)
            if ($size -eq 0) { break }
            $out.Write($bodyBytes, $crlf + 2, $size)
            $pos = $crlf + 2 + $size + 2
        }
        return [Text.Encoding]::UTF8.GetString($out.ToArray())
    }
    return [Text.Encoding]::UTF8.GetString($bodyBytes)
}

function Get-Groups {
    $raw = Invoke-Api -Method 'GET' -Path '/proxies'
    if (-not $raw) { return @{} }
    $data = $raw | ConvertFrom-Json
    $groups = @{}
    foreach ($name in $data.proxies.PSObject.Properties.Name) {
        $p = $data.proxies.$name
        if ($p.type -in @('Selector', 'URLTest', 'Fallback', 'LoadBalance')) {
            $groups[$name] = @{ type = $p.type; now = $p.now; nodes = @($p.all) }
        }
    }
    return $groups
}

switch ($Action) {
    'list' {
        $groups = Get-Groups
        if ($groups.Count -eq 0) { Write-Output '(无代理组或读取失败)' }
        foreach ($g in $groups.Keys | Sort-Object) {
            $info = $groups[$g]
            Write-Output ("[$($info.type)] $g -> $($info.now)  ($($info.nodes.Count) 节点)")
        }
    }
    'groups' {
        $groups = Get-Groups
        $groups.Keys | Sort-Object | ForEach-Object { Write-Output $_ }
    }
    'nodes' {
        $groups = Get-Groups
        if (-not $groups.ContainsKey($Group)) { Write-Output "组不存在: $Group"; exit 1 }
        $groups[$Group].nodes | ForEach-Object { Write-Output $_ }
    }
    'current' {
        $groups = Get-Groups
        if (-not $groups.ContainsKey($Group)) { Write-Output "组不存在: $Group"; exit 1 }
        Write-Output $groups[$Group].now
    }
    'switch' {
        $groups = Get-Groups
        if (-not $groups.ContainsKey($Group)) { Write-Output "组不存在: $Group"; exit 1 }
        $encGroup = [Uri]::EscapeDataString($Group)
        $body = "{`"name`":`"$Node`"}"
        Invoke-Api -Method 'PUT' -Path "/proxies/$encGroup" -Body $body | Out-Null
        Write-Output "已切换 $Group -> $Node"
    }
    'rotate' {
        $groups = Get-Groups
        if (-not $groups.ContainsKey($Group)) { Write-Output "组不存在: $Group"; exit 1 }
        $nodes = $groups[$Group].nodes
        $cur = $groups[$Group].now
        if ($nodes.Count -le 1) { Write-Output "节点不足，保持 $cur"; exit 0 }
        $candidates = @($nodes | Where-Object { $_ -ne $cur })
        if ($candidates.Count -eq 0) { Write-Output "无其他节点，保持 $cur"; exit 0 }
        $next = $candidates | Get-Random
        $encGroup = [Uri]::EscapeDataString($Group)
        $body = "{`"name`":`"$next`"}"
        Invoke-Api -Method 'PUT' -Path "/proxies/$encGroup" -Body $body | Out-Null
        Write-Output $next
    }
    default { Write-Output "未知动作: $Action" }
}
