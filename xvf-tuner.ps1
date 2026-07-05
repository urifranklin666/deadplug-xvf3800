# DEADPLUG // XVF3800 TUNER
# WPF slider panel for ReSpeaker XVF3800 tuning via xvf_host.exe
# Launch with: powershell -NoProfile -STA -ExecutionPolicy Bypass -File xvf-tuner.ps1

Add-Type -AssemblyName PresentationFramework, PresentationCore, WindowsBase

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$script:xvf = Join-Path $here 'host_control\win32\xvf_host.exe'
if (-not (Test-Path $script:xvf)) {
    [Windows.MessageBox]::Show("xvf_host.exe not found at:`n$script:xvf", "DEADPLUG // XVF3800") | Out-Null
    exit 1
}

$inv = [Globalization.CultureInfo]::InvariantCulture

function Invoke-Xvf {
    param([string[]]$XvfArgs)
    try { & $script:xvf @XvfArgs 2>$null | Where-Object { $_ -notmatch 'device_init' } }
    catch { $null }
}

function Get-XvfParam {
    param([string]$Name)
    $out = @(Invoke-Xvf @($Name)) -join ' '
    if ($out -match "$Name\s+(-?[\d\.eE\-\+]+)") { return [double]::Parse($Matches[1], $inv) }
    return $null
}

$script:params = @(
    @{ Name='AUDIO_MGR_MIC_GAIN';  Label='MIC GAIN';            Min=0;     Max=255;  Fmt='0';      Digits=0 },
    @{ Name='PP_AGCMAXGAIN';       Label='AGC MAX GAIN';        Min=1;     Max=500;  Fmt='0';      Digits=0 },
    @{ Name='PP_AGCDESIREDLEVEL';  Label='AGC TARGET LEVEL';    Min=0.001; Max=0.02; Fmt='0.0000'; Digits=4 },
    @{ Name='PP_AGCTIME';          Label='AGC RAMP TIME (S)';   Min=0.1;   Max=3;    Fmt='0.00';   Digits=2 },
    @{ Name='PP_AGCFASTTIME';      Label='AGC PEAK RECOVERY (S)'; Min=0.05; Max=1;   Fmt='0.00';   Digits=2 },
    @{ Name='PP_MIN_NS';           Label='NS FLOOR / STATIONARY'; Min=0;   Max=1;    Fmt='0.00';   Digits=2 },
    @{ Name='PP_MIN_NN';           Label='NS FLOOR / NON-STAT';   Min=0;   Max=1;    Fmt='0.00';   Digits=2 }
)
$script:paramsByName = @{}
foreach ($p in $script:params) { $script:paramsByName[$p.Name] = $p }

$script:presets = @{
    STOCK   = @{ AUDIO_MGR_MIC_GAIN=90; PP_AGCMAXGAIN=64;  PP_AGCDESIREDLEVEL=0.0045; PP_AGCTIME=0.9; PP_AGCFASTTIME=0.1; PP_MIN_NS=0.15; PP_MIN_NN=0.51 }
    WHISPER = @{ AUDIO_MGR_MIC_GAIN=90; PP_AGCMAXGAIN=160; PP_AGCDESIREDLEVEL=0.007;  PP_AGCTIME=0.9; PP_AGCFASTTIME=0.2; PP_MIN_NS=0.35; PP_MIN_NN=0.6 }
}

$xaml = @'
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="DEADPLUG // XVF3800" Width="600" Height="780"
        Background="#050505" WindowStartupLocation="CenterScreen"
        FontFamily="Consolas">
  <Window.Resources>
    <Style TargetType="TextBlock">
      <Setter Property="Foreground" Value="#c9c9c9"/>
      <Setter Property="FontFamily" Value="Consolas"/>
    </Style>
    <Style x:Key="FlatRepeat" TargetType="RepeatButton">
      <Setter Property="Focusable" Value="False"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="RepeatButton">
            <Border Background="{TemplateBinding Background}" Height="4"/>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
    <Style x:Key="DPSlider" TargetType="Slider">
      <Setter Property="Height" Value="24"/>
      <Setter Property="IsMoveToPointEnabled" Value="True"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Slider">
            <Grid VerticalAlignment="Center">
              <Track x:Name="PART_Track">
                <Track.DecreaseRepeatButton>
                  <RepeatButton Style="{StaticResource FlatRepeat}" Background="#ff2222" Command="Slider.DecreaseLarge"/>
                </Track.DecreaseRepeatButton>
                <Track.IncreaseRepeatButton>
                  <RepeatButton Style="{StaticResource FlatRepeat}" Background="#2a0d0d" Command="Slider.IncreaseLarge"/>
                </Track.IncreaseRepeatButton>
                <Track.Thumb>
                  <Thumb Focusable="False">
                    <Thumb.Template>
                      <ControlTemplate TargetType="Thumb">
                        <Border Width="9" Height="20" Background="#ff2222"/>
                      </ControlTemplate>
                    </Thumb.Template>
                  </Thumb>
                </Track.Thumb>
              </Track>
            </Grid>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
    <Style TargetType="Button">
      <Setter Property="Background" Value="#120606"/>
      <Setter Property="Foreground" Value="#ff4444"/>
      <Setter Property="BorderBrush" Value="#7a0f0f"/>
      <Setter Property="FontFamily" Value="Consolas"/>
      <Setter Property="FontSize" Value="12"/>
      <Setter Property="Padding" Value="12,6"/>
      <Setter Property="Margin" Value="0,0,8,0"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Border Background="{TemplateBinding Background}" BorderBrush="{TemplateBinding BorderBrush}"
                    BorderThickness="1" Padding="{TemplateBinding Padding}">
              <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter Property="Background" Value="#2a0a0a"/>
                <Setter Property="Foreground" Value="#ff6666"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
    <Style TargetType="CheckBox">
      <Setter Property="Foreground" Value="#ff4444"/>
      <Setter Property="FontFamily" Value="Consolas"/>
    </Style>
  </Window.Resources>
  <DockPanel Margin="16">
    <Grid DockPanel.Dock="Top" Margin="0,0,0,10">
      <TextBlock Text="DEADPLUG // XVF3800" FontSize="20" FontWeight="Bold" Foreground="#ff2222"/>
      <TextBlock x:Name="DeviceStatus" Text="..." HorizontalAlignment="Right" VerticalAlignment="Bottom" Foreground="#666666"/>
    </Grid>
    <Border DockPanel.Dock="Top" Height="1" Background="#7a0f0f" Margin="0,0,0,12"/>

    <StackPanel DockPanel.Dock="Top" x:Name="SliderPanel"/>

    <Grid DockPanel.Dock="Top" Margin="0,10,0,0">
      <Grid.ColumnDefinitions>
        <ColumnDefinition Width="Auto"/>
        <ColumnDefinition Width="*"/>
        <ColumnDefinition Width="Auto"/>
      </Grid.ColumnDefinitions>
      <CheckBox x:Name="AgcOn" Grid.Column="0" Content="AGC" VerticalAlignment="Center" Margin="0,0,14,0"/>
      <ProgressBar x:Name="AgcMeter" Grid.Column="1" Height="14" Minimum="1" Maximum="160"
                   Background="#160808" Foreground="#ff2222" BorderBrush="#7a0f0f" BorderThickness="1"/>
      <TextBlock x:Name="AgcMeterText" Grid.Column="2" Margin="10,0,0,0" VerticalAlignment="Center" Foreground="#ff4444" Text="GAIN --"/>
    </Grid>

    <StackPanel DockPanel.Dock="Top" Orientation="Horizontal" Margin="0,14,0,0">
      <Button x:Name="BtnStock" Content="STOCK"/>
      <Button x:Name="BtnWhisper" Content="WHISPER"/>
      <Button x:Name="BtnReload" Content="RELOAD"/>
      <Button x:Name="BtnSave" Content="SAVE TO FLASH" Foreground="#ffaa00" BorderBrush="#aa6600"/>
    </StackPanel>

    <TextBox x:Name="LogBox" Margin="0,14,0,0" IsReadOnly="True" TextWrapping="Wrap"
             VerticalScrollBarVisibility="Auto" Background="#0a0a0a" Foreground="#9a3d3d"
             BorderBrush="#3a0a0a" BorderThickness="1" FontFamily="Consolas" FontSize="11"/>
  </DockPanel>
</Window>
'@

$window = [Windows.Markup.XamlReader]::Parse($xaml)
$sliderPanel = $window.FindName('SliderPanel')
$logBox      = $window.FindName('LogBox')
$agcMeter    = $window.FindName('AgcMeter')
$agcMeterTxt = $window.FindName('AgcMeterText')
$agcOn       = $window.FindName('AgcOn')
$devStatus   = $window.FindName('DeviceStatus')

$script:suppress = $false
$script:pending  = @{}
$script:sliders  = @{}
$script:valueLabels = @{}

function Write-Log {
    param([string]$Msg)
    $logBox.AppendText("[$(Get-Date -Format HH:mm:ss)] $Msg`r`n")
    $logBox.ScrollToEnd()
}

$sliderHandler = {
    param($sender, $e)
    $name = [string]$sender.Tag
    $p = $script:paramsByName[$name]
    $v = [math]::Round($sender.Value, $p.Digits)
    $txt = $v.ToString($p.Fmt, [Globalization.CultureInfo]::InvariantCulture)
    $script:valueLabels[$name].Text = $txt
    if ($name -eq 'PP_AGCMAXGAIN') { $agcMeter.Maximum = [math]::Max($v, 2) }
    if (-not $script:suppress) { $script:pending[$name] = $txt }
}

foreach ($p in $script:params) {
    $row = New-Object Windows.Controls.Grid
    $row.Margin = '0,0,0,10'
    $c1 = New-Object Windows.Controls.ColumnDefinition
    $c2 = New-Object Windows.Controls.ColumnDefinition; $c2.Width = 'Auto'
    $row.ColumnDefinitions.Add($c1); $row.ColumnDefinitions.Add($c2)
    $r1 = New-Object Windows.Controls.RowDefinition
    $r2 = New-Object Windows.Controls.RowDefinition
    $row.RowDefinitions.Add($r1); $row.RowDefinitions.Add($r2)

    $lbl = New-Object Windows.Controls.TextBlock
    $lbl.Text = $p.Label; $lbl.FontSize = 12; $lbl.Foreground = '#c9c9c9'
    [Windows.Controls.Grid]::SetRow($lbl, 0); [Windows.Controls.Grid]::SetColumn($lbl, 0)

    $val = New-Object Windows.Controls.TextBlock
    $val.Text = '--'; $val.FontSize = 12; $val.Foreground = '#ff4444'
    [Windows.Controls.Grid]::SetRow($val, 0); [Windows.Controls.Grid]::SetColumn($val, 1)

    $s = New-Object Windows.Controls.Slider
    $s.Minimum = $p.Min; $s.Maximum = $p.Max
    $s.Style = $window.Resources['DPSlider']
    $s.Tag = $p.Name
    $s.Add_ValueChanged($sliderHandler)
    [Windows.Controls.Grid]::SetRow($s, 1)
    [Windows.Controls.Grid]::SetColumnSpan($s, 2)

    $row.Children.Add($lbl) | Out-Null
    $row.Children.Add($val) | Out-Null
    $row.Children.Add($s)   | Out-Null
    $sliderPanel.Children.Add($row) | Out-Null

    $script:sliders[$p.Name] = $s
    $script:valueLabels[$p.Name] = $val
}

function Sync-FromDevice {
    $script:suppress = $true
    $ok = $false
    foreach ($p in $script:params) {
        $v = Get-XvfParam $p.Name
        if ($null -ne $v) {
            $ok = $true
            $script:sliders[$p.Name].Value = [math]::Min([math]::Max($v, $p.Min), $p.Max)
        }
    }
    $agc = Get-XvfParam 'PP_AGCONOFF'
    if ($null -ne $agc) { $agcOn.IsChecked = ($agc -ge 1) }
    $script:suppress = $false
    if ($ok) {
        $devStatus.Text = 'DEVICE ONLINE'; $devStatus.Foreground = '#ff2222'
        Write-Log 'Loaded current values from device.'
    } else {
        $devStatus.Text = 'DEVICE OFFLINE'; $devStatus.Foreground = '#666666'
        Write-Log 'ERROR: no response from device. Is it plugged in?'
    }
    return $ok
}

function Set-Preset {
    param([string]$Name)
    $pre = $script:presets[$Name]
    foreach ($k in $pre.Keys) {
        $script:sliders[$k].Value = $pre[$k]   # not suppressed -> queues writes
    }
    Write-Log "Preset applied: $Name"
}

# Debounced writer: coalesces slider movement into one write per param every 300 ms
$applyTimer = New-Object Windows.Threading.DispatcherTimer
$applyTimer.Interval = [TimeSpan]::FromMilliseconds(300)
$applyTimer.Add_Tick({
    foreach ($k in @($script:pending.Keys)) {
        $v = $script:pending[$k]
        $script:pending.Remove($k)
        Invoke-Xvf @($k, $v) | Out-Null
        Write-Log "SET $k $v"
    }
})

# Live AGC gain meter (1 s poll)
$meterTimer = New-Object Windows.Threading.DispatcherTimer
$meterTimer.Interval = [TimeSpan]::FromSeconds(1)
$meterTimer.Add_Tick({
    if ($script:pending.Count -gt 0) { return }  # don't contend with writes
    $g = Get-XvfParam 'PP_AGCGAIN'
    if ($null -ne $g) {
        $agcMeter.Value = [math]::Min([math]::Max($g, $agcMeter.Minimum), $agcMeter.Maximum)
        $agcMeterTxt.Text = 'GAIN ' + $g.ToString('0.0', [Globalization.CultureInfo]::InvariantCulture)
    }
})

$agcOn.Add_Click({
    $v = 0; if ($agcOn.IsChecked) { $v = 1 }
    Invoke-Xvf @('PP_AGCONOFF', "$v") | Out-Null
    Write-Log "SET PP_AGCONOFF $v"
})

$window.FindName('BtnStock').Add_Click({ Set-Preset 'STOCK' })
$window.FindName('BtnWhisper').Add_Click({ Set-Preset 'WHISPER' })
$window.FindName('BtnReload').Add_Click({ Sync-FromDevice | Out-Null })
$window.FindName('BtnSave').Add_Click({
    Invoke-Xvf @('save_configuration', '1') | Out-Null
    Write-Log 'Configuration saved to device flash (survives replug).'
})

Sync-FromDevice | Out-Null
$applyTimer.Start()
$meterTimer.Start()

if ($env:XVF_TUNER_SMOKETEST -eq '1') {
    $summary = foreach ($p in $script:params) { "$($p.Name)=$($script:valueLabels[$p.Name].Text)" }
    Write-Output ("SMOKETEST OK | " + ($summary -join ' '))
    exit 0
}

$window.ShowDialog() | Out-Null
