<#
.SYNOPSIS
    Bring the NeuroSky MindWave Mobile up on Windows and verify it is actually streaming.

.DESCRIPTION
    Works around the failure modes this headset hits on Windows 11:

      * The outgoing COM port RENUMBERS on every re-pair, so it is discovered by
        the headset's Bluetooth address, never hardcoded.
      * Windows also creates an *incoming* Bluetooth COM port that opens instantly
        and delivers nothing. Drivers that scan COM1..16 latch onto it and fail.
        Only the port bound to the headset address is real.
      * ThinkGear Connector autostarts and holds the port exclusively, which makes
        every other client fail. It is stopped unless -KeepTGC is passed.
      * A second Bluetooth radio (e.g. a CSR dongle) puts the stack into
        CM_PROB_FAILED_ADD and silently invalidates link keys.

    Exit code 0 means a verified ThinkGear stream. Anything else is a failure.

.PARAMETER Repair
    Remove the pairing so you can build it again. Pair again through Windows Settings
    using PIN 0000. Do NOT let Windows pick its default Secure Simple Pairing
    path, and do not script the pairing. BluetoothAuthenticateDevice reports
    success but produces a port whose RFCOMM connect never completes.

.PARAMETER Run
    On success, hand the discovered port to mindwave_live.py.

.EXAMPLE
    .\mindwave.ps1
.EXAMPLE
    .\mindwave.ps1 -Run
.EXAMPLE
    .\mindwave.ps1 -Repair
#>
[CmdletBinding()]
param(
    [switch]$Repair,
    [switch]$KeepTGC,
    [switch]$StartTGC,
    [switch]$Run,
    [int]$Seconds = 10,
    [string]$Address = 'a4da326ff117'
)

# System.IO.Ports is not present in PowerShell 7 by default; re-launch under 5.1.
if ($PSVersionTable.PSEdition -eq 'Core') {
    $relaunch = @('-NoProfile','-ExecutionPolicy','Bypass','-File',$PSCommandPath)
    foreach ($kv in $PSBoundParameters.GetEnumerator()) {
        if ($kv.Value -is [switch]) { if ($kv.Value.IsPresent) { $relaunch += "-$($kv.Key)" } }
        else { $relaunch += "-$($kv.Key)"; $relaunch += [string]$kv.Value }
    }
    & "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" $relaunch
    exit $LASTEXITCODE
}

$ErrorActionPreference = 'Stop'
$AddrHex = $Address.ToUpper()
$AddrNum = [uint64]('0x' + $Address)

function Say  ($m) { Write-Host "     $m" }
function Ok   ($m) { Write-Host "[ ok ] $m" -ForegroundColor Green }
function Warn ($m) { Write-Host "[warn] $m" -ForegroundColor Yellow }
function Bad  ($m) { Write-Host "[FAIL] $m" -ForegroundColor Red }
function Step ($m) { Write-Host ""; Write-Host "--- $m" -ForegroundColor Cyan }

if (-not ('MW.Bt' -as [type])) {
$csharp = @"
using System;
using System.Runtime.InteropServices;
namespace MW {
  public static class Bt {
    [StructLayout(LayoutKind.Sequential)] public struct ST { public ushort y,mo,dow,d,h,mi,s,ms; }
    [StructLayout(LayoutKind.Sequential)]
    public struct SEARCH { public int dwSize;
      [MarshalAs(UnmanagedType.Bool)] public bool fAuth;
      [MarshalAs(UnmanagedType.Bool)] public bool fRem;
      [MarshalAs(UnmanagedType.Bool)] public bool fUnk;
      [MarshalAs(UnmanagedType.Bool)] public bool fCon;
      [MarshalAs(UnmanagedType.Bool)] public bool fInq;
      public byte cTimeout; public IntPtr hRadio; }
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
    public struct INFO { public int dwSize; public ulong Address; public uint cod;
      [MarshalAs(UnmanagedType.Bool)] public bool fConnected;
      [MarshalAs(UnmanagedType.Bool)] public bool fRemembered;
      [MarshalAs(UnmanagedType.Bool)] public bool fAuthenticated;
      public ST seen; public ST used;
      [MarshalAs(UnmanagedType.ByValTStr, SizeConst=248)] public string szName; }
    [DllImport("bthprops.cpl", SetLastError=true)] public static extern IntPtr BluetoothFindFirstDevice(ref SEARCH p, ref INFO i);
    [DllImport("bthprops.cpl", SetLastError=true)] public static extern bool   BluetoothFindNextDevice(IntPtr h, ref INFO i);
    [DllImport("bthprops.cpl", SetLastError=true)] public static extern bool   BluetoothFindDeviceClose(IntPtr h);
    [DllImport("bthprops.cpl", SetLastError=true)] public static extern uint   BluetoothRemoveDevice(ref ulong a);
  }
}
"@
Add-Type -TypeDefinition $csharp
}

function Get-Headset {
    param([byte]$TimeoutMult = 8)
    $s = New-Object MW.Bt+SEARCH
    $s.dwSize = [Runtime.InteropServices.Marshal]::SizeOf([type][MW.Bt+SEARCH])
    $s.fUnk = $true; $s.fAuth = $true; $s.fRem = $true; $s.fCon = $true; $s.fInq = $true
    $s.cTimeout = $TimeoutMult; $s.hRadio = [IntPtr]::Zero
    $i = New-Object MW.Bt+INFO
    $i.dwSize = [Runtime.InteropServices.Marshal]::SizeOf([type][MW.Bt+INFO])
    $h = [MW.Bt]::BluetoothFindFirstDevice([ref]$s, [ref]$i)
    if ($h -eq [IntPtr]::Zero) { return $null }
    $found = $null
    do {
        if ($i.Address -eq $AddrNum) { $found = $i; break }
        $i = New-Object MW.Bt+INFO
        $i.dwSize = [Runtime.InteropServices.Marshal]::SizeOf([type][MW.Bt+INFO])
    } while ([MW.Bt]::BluetoothFindNextDevice($h, [ref]$i))
    [void][MW.Bt]::BluetoothFindDeviceClose($h)
    return $found
}

function Get-HeadsetPort {
    $dev = Get-PnpDevice -Class Ports -ErrorAction SilentlyContinue |
           Where-Object { $_.Present -and $_.InstanceId -match $AddrHex }
    if (-not $dev) { return $null }
    return ([regex]'\((COM\d+)\)').Match(@($dev)[0].FriendlyName).Groups[1].Value
}

function Stop-TGC {
    $procs = @(Get-Process -ErrorAction SilentlyContinue |
               Where-Object { $_.ProcessName -match 'think|neuro' })
    foreach ($p in $procs) { Say "stopping $($p.ProcessName) (pid $($p.Id))"; Stop-Process -Id $p.Id -Force }
    if ($procs.Count) { Start-Sleep -Seconds 3 }
    return $procs.Count
}

# Read the port and decode the ThinkGear packet stream.
function Test-Stream {
    param([string]$Com, [int]$Duration)
    $r = [pscustomobject]@{
        Opened = $false; OpenSeconds = 0.0; Error = $null
        Bytes = 0; Packets = 0; BadChecksum = 0
        Poor = $null; Attention = $null; Meditation = $null
        RawSamples = 0; BandFrames = 0
    }
    $sp = New-Object System.IO.Ports.SerialPort $Com,57600,'None',8,'One'
    $sp.ReadTimeout = 2000
    $t0 = Get-Date
    try { $sp.Open() }
    catch {
        $r.OpenSeconds = ((Get-Date) - $t0).TotalSeconds
        $r.Error = ($_.Exception.Message -replace '\s+',' ').Trim()
        return $r
    }
    $r.Opened = $true
    $r.OpenSeconds = ((Get-Date) - $t0).TotalSeconds

    $chunks = New-Object System.Collections.Generic.List[byte]
    $buf = New-Object byte[] 4096
    $deadline = (Get-Date).AddSeconds($Duration)
    while ((Get-Date) -lt $deadline) {
        try { $n = $sp.Read($buf, 0, $buf.Length) } catch { continue }
        for ($k = 0; $k -lt $n; $k++) { $chunks.Add($buf[$k]) }
    }
    $sp.Close()

    $b = $chunks.ToArray()
    $r.Bytes = $b.Length
    $i = 0
    while ($i -lt $b.Length - 3) {
        if ($b[$i] -ne 0xAA -or $b[$i+1] -ne 0xAA) { $i++; continue }
        $pl = $b[$i+2]
        if ($pl -gt 169 -or $pl -eq 0) { $i++; continue }
        if ($i + 3 + $pl -ge $b.Length) { break }
        $payload = @($b[($i+3)..($i+2+$pl)])
        $sum = 0; foreach ($x in $payload) { $sum += $x }
        if ((((-bnot ($sum -band 0xFF))) -band 0xFF) -ne $b[$i+3+$pl]) { $r.BadChecksum++; $i++; continue }
        $r.Packets++
        $j = 0
        while ($j -lt $payload.Count) {
            while ($j -lt $payload.Count -and $payload[$j] -eq 0x55) { $j++ }
            if ($j -ge $payload.Count) { break }
            $code = $payload[$j]; $j++
            if ($code -lt 0x80) {
                if ($j -ge $payload.Count) { break }
                $v = $payload[$j]; $j++
                switch ($code) { 2 { $r.Poor = $v } 4 { $r.Attention = $v } 5 { $r.Meditation = $v } }
            } else {
                if ($j -ge $payload.Count) { break }
                $vl = $payload[$j]; $j++
                if ($code -eq 0x80) { $r.RawSamples++ }
                if ($code -eq 0x83) { $r.BandFrames++ }
                $j += $vl
            }
        }
        $i += 4 + $pl
    }
    return $r
}

# ----------------------------------------------------------------------------

Write-Host ""
Write-Host "MindWave Mobile  $AddrHex" -ForegroundColor White

Step "Bluetooth radios"
$radios = @(Get-PnpDevice -ErrorAction SilentlyContinue |
            Where-Object { $_.Service -eq 'BTHUSB' -and $_.Present })
foreach ($rd in $radios) {
    if ($rd.Status -eq 'OK') { Ok $rd.FriendlyName }
    else { Bad "$($rd.FriendlyName): $($rd.Problem)" }
}
if ($radios.Count -gt 1) {
    Warn "$($radios.Count) radios present. Windows supports ONE."
    Warn "Unplug the spare dongle: it invalidates link keys and breaks pairing."
}
if ($radios.Count -eq 0) { Bad "no Bluetooth radio"; exit 3 }

if (-not $KeepTGC) {
    Step "ThinkGear Connector"
    if ((Stop-TGC) -gt 0) { Ok "stopped (it holds the COM port exclusively)" }
    else { Say "not running" }
}

if ($Repair) {
    Step "Removing pairing"
    $a = $AddrNum
    $rc = [MW.Bt]::BluetoothRemoveDevice([ref]$a)
    if ($rc -eq 0) { Ok "pairing removed" }
    else { Warn "rc=$rc $((New-Object ComponentModel.Win32Exception([int]$rc)).Message)" }
    Start-Sleep -Seconds 4
    Write-Host ""
    Write-Host "  Now re-pair BY HAND:" -ForegroundColor White
    Write-Host "    1. Power-cycle the headset (off ~10s, on)."
    Write-Host "    2. Settings > Bluetooth & devices > Add device > Bluetooth."
    Write-Host "    3. Pick MindWave Mobile. When asked for a PIN enter 0000."
    Write-Host ""
    Write-Host "  Do not script this step. BluetoothAuthenticateDevice returns success" -ForegroundColor DarkGray
    Write-Host "  but yields a port whose RFCOMM connect never completes." -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "  Then run:  .\mindwave.ps1" -ForegroundColor White
    exit 0
}

Step "Headset on air"
$hs = Get-Headset
if (-not $hs) {
    Bad "not found in inquiry scan"
    Say "It is powered off, out of range, or the battery is flat."
    Say "This headset advertises whenever it is switched on. There is no pairing mode."
    exit 4
}
Ok ("{0}  paired={1}  connected={2}" -f $hs.szName, $hs.fAuthenticated, $hs.fConnected)
if (-not $hs.fAuthenticated) {
    Bad "not paired"
    Say "Run:  .\mindwave.ps1 -Repair"
    exit 5
}

Step "Outgoing COM port"
$com = Get-HeadsetPort
if (-not $com) {
    Bad "no port bound to $AddrHex"
    Say "Pairing exists but Windows did not create the serial port. Re-pair:"
    Say "  .\mindwave.ps1 -Repair"
    exit 6
}
Ok "$com is bound to the headset"
$others = @(Get-PnpDevice -Class Ports -ErrorAction SilentlyContinue |
            Where-Object { $_.Present -and $_.InstanceId -notmatch $AddrHex -and $_.FriendlyName -match 'Bluetooth' })
foreach ($o in $others) {
    $on = ([regex]'\((COM\d+)\)').Match($o.FriendlyName).Groups[1].Value
    Warn "$on is an INCOMING port. Opens instantly, never sends data. Ignore it."
}

Step "Reading $com for ${Seconds}s"
$res = Test-Stream -Com $com -Duration $Seconds
if (-not $res.Opened) {
    Bad ("open failed after {0:N1}s: {1}" -f $res.OpenSeconds, $res.Error)
    Write-Host ""
    if ($res.Error -match 'semaphore') {
        Say "'Semaphore timeout' = the headset never answered the RFCOMM connect."
        Say "The pairing is fine; the headset is refusing the channel. In order:"
        Say "  1. Power-cycle the headset. It serves ONE connection and a failed"
        Say "     attempt can leave that slot wedged."
        Say "  2. Fresh AAA. It still advertises and pairs on a weak cell but"
        Say "     browns out at the data link."
        Say "  3. Disconnect it from any phone or tablet holding it."
        Say "  4. .\mindwave.ps1 -Repair   (re-pair with PIN 0000)"
    } elseif ($res.Error -match 'denied') {
        Say "Another process owns $com. Usually ThinkGear Connector; re-run without -KeepTGC."
    }
    exit 7
}
Ok ("opened in {0:N1}s" -f $res.OpenSeconds)

if ($res.Packets -eq 0) {
    Bad "port opened but no valid ThinkGear packets ($($res.Bytes) bytes, $($res.BadChecksum) bad checksums)"
    if ($res.Bytes -eq 0) { Say "Silent port. If this is not the headset-bound port, re-pair." }
    exit 8
}

$rate = [math]::Round($res.Bytes / $Seconds)
Ok "$($res.Packets) packets, $($res.Bytes) bytes (~$rate B/s), $($res.RawSamples) raw samples, $($res.BandFrames) band frames"
if ($res.BadChecksum -gt 0) { Warn "$($res.BadChecksum) bad checksums (mild RF interference)" }

Step "Signal quality"
if ($null -ne $res.Poor) {
    if     ($res.Poor -eq 0)   { Ok   "poorSignal 0: clean contact" }
    elseif ($res.Poor -ge 200) { Warn "poorSignal 200: no skin contact" }
    else                       { Warn "poorSignal $($res.Poor): partial contact" }
    if ($res.Poor -gt 0) {
        Say "Sensor arm flat on bare forehead above the eyebrow."
        Say "Ear clip fully on the lobe, both metal pads touching skin."
        Say "attention/meditation stay 0 until this reaches 0."
    }
} else { Say "no poorSignal row seen yet" }
if ($null -ne $res.Attention)  { Say "attention  $($res.Attention)" }
if ($null -ne $res.Meditation) { Say "meditation $($res.Meditation)" }

Write-Host ""
Ok "LINK VERIFIED on $com"
Write-Host "PORT=$com"

if ($Run) {
    Step "Launching mindwave_live.py"
    Push-Location $PSScriptRoot
    try { & python mindwave_live.py --port $com }
    finally { Pop-Location }
}
elseif ($StartTGC) {
    Step "Starting ThinkGear Connector"
    $tgc = 'E:\ThinkGear_Connector\ThinkGear Connector.exe'
    if (Test-Path $tgc) {
        Start-Process -FilePath $tgc -WorkingDirectory (Split-Path $tgc)
        Ok "started, serving on 127.0.0.1:13854"
        Warn "it now owns $com; nothing else can open the port"
    } else { Bad "not found at $tgc" }
}

exit 0
