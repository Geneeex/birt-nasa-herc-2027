# Pi 5 setup and operation

Revision 5, 23 November 2026. Raspberry Pi 5 Model B 8GB is the only task computer. Python runs the radio interpreter, camera, image processing, calibrated pan control, receiver arming and evidence display. A dedicated PCA9685 board produces servo pulses over I2C. There is no Arduino dependency, firmware upload or USB serial controller.

This package implements the autonomous camera task. It does not implement propulsion, steering, a physical filter wheel or a LiDAR driver. Red and blue filters are digital channel selection; distance comes from calibrated camera geometry. Live use remains conditional on organizer confirmation of those interpretations and physical qualification.

## Required hardware and pin allocation

Use Raspberry Pi OS 64-bit on the Pi 5, Camera Module 3 Standard with a 22-to-15-pin ribbon, the selected USB SDR, MG996R pan servo, a dedicated PCA9685 at address 0x40, a separate regulated 5 V servo supply and the FlySky receiver. The 8GB memory variant does not change the GPIO interface.

The numbers called GPIO below are BCM header offsets on the RP1 controller, not physical pin numbers. The code discovers the RP1 controller by label; it does not assume gpiochip0 or gpiochip4.

| Connection | Pi physical pin | Function |
| --- | --- | --- |
| PCA9685 VCC | 1 | 3.3 V logic only |
| PCA9685 SDA | 3 | GPIO2, I2C1 data |
| PCA9685 SCL | 5 | GPIO3, I2C1 clock |
| Common signal ground | 6 | Pi, driver, receiver and UBEC ground |
| PCA9685 OE | 11 | GPIO17; high disables all driver outputs |
| Bench jumper to ground | 15 | GPIO22; low selects BENCH at process startup |
| Protected CH6 signal | 18 | GPIO24 input, strictly 3.3 V logic |

PCA9685 channel 0 signal goes to the servo. Servo positive goes to the UBEC 5 V rail and PCA9685 V+; negative goes directly to the UBEC return and driver ground. A common signal ground is necessary, but servo current must not return through the Pi. Never join servo V+ to Pi 3.3 V or 5 V. Keep the two battery positive rails separate.

Before connection, remove the PCA9685 board's OE pull-down and fit an external 10 kΩ OE-to-3.3 V pull-up. Verify OE is high and outputs are inactive with Pi software stopped, during boot and after process termination. An unmodified board normally enables its outputs by default. OE affects every channel; reserve the entire board for this task. Do not connect propulsion or steering to this board. Other rover software must not claim GPIO17, GPIO22, GPIO24 or reset this PCA9685.

Protect CH6 using a non-inverting SN74LVC1G17 buffer powered from Pi 3.3 V. CH6 goes to A, Y goes through 220 Ω to GPIO24, and grounds are common. Fit 100 kΩ from A to ground and 100 nF between buffer VCC and ground. Verify the delivered package pinout. Measure CH6 and keep its input within 0–5.5 V; receiver supply voltage is not the same as signal voltage. Do not apply a 5 V PWM signal directly to a Pi GPIO. Verify the servo recognizes the driver's 3.3 V signal before installing its horn.

## Install on the Pi

Copy the source folder to a path without spaces, for example /home/yourname/HERC_task. Use Python 3.11 or newer. Run these commands on the Pi:

```bash
sudo apt update
sudo apt install python3-picamera2 python3-opencv python3-numpy python3-pil python3-lgpio i2c-tools gpiod rtl-sdr direwolf
sudo raspi-config nonint do_i2c 0
sudo usermod -aG gpio,i2c,video "$USER"
```

Reboot in the pit after enabling I2C and group membership. Use the OS camera and lgpio packages together; this implementation does not use pigpio or the older RPi.GPIO library. No pip camera replacement is required. Verify GPIO ownership and the Pi model:

```bash
cat /proc/device-tree/model
getconf LONG_BIT
gpioinfo
ls -l /dev/i2c-1 /dev/gpiochip*
rpicam-hello --list-cameras
python3 -c "import lgpio, picamera2, cv2, numpy, PIL; print('Imports OK')"
cp config.example.json config.json
python3 -m tools.check_hardware --config config.json --seconds 30
```

The hardware check opens the task driver with OE disabled and observes arming; it never commands a servo pulse. Keep the servo horn removed for initial tests. If access is denied, check group membership and device ownership instead of running the whole mission as root. Keep live services stopped during commissioning.

Configure schema 2, hardware pins/address, measured pan/camera calibrations, confirmed radio parameters and trusted clock data. Blank NASA fields are intentional. KISS port 8001 must match deploy/direwolf.conf. Only one program may own the SDR or task GPIO resources. Run rtl_test -t separately and stop it before starting reception.

## Calibrate

Fit the GPIO22-to-ground jumper before launching the pan tool. BENCH is deliberate local setup; never leave the jumper fitted for a course run.

```bash
python3 -m tools.calibrate_pan --config config.json
python3 -m tools.calibrate_camera capture --images calibration/images --count 25 --lens-position 0.0
python3 -m tools.calibrate_camera fit --images calibration/images --columns 9 --rows 6 --square-mm YOUR_MEASURED_PITCH --output calibration/camera.json
```

The pan tool limits adjustments to 50 microseconds per step and sessions to 180 seconds. Start horn-off, then power down before fitting the centred horn. Record actual viewing angles from both directions, forward zero, travel endpoints and settling time. Typical 50 Hz PWM has about 4.88 microseconds per PCA9685 step. Its 25 MHz clock is nominal: measure pulse period/width, update reference_clock_hz if required, and repeat the angle calibration after any clock change. A pulse command is not an angle measurement.

Camera lens position 0.0 is only an initial setting. Establish sharpness, then keep focus, crop and resolution fixed. Use a measured 10-by-7-square checkerboard with 9-by-6 inner corners and at least 20 diverse usable views. The optional near-range fallback stays disabled until qualified. Test real targets outdoors against independent tape measurements.

## Software and physical bench tests

The first three commands below can run on a development computer without Pi hardware. The simulation labels all results and never opens GPIO, a camera or an SDR.

```bash
python3 -m unittest discover -s tests -v
python3 -m tools.simulate --output demo_runs
python3 -m herc.review --root demo_runs
```

Open http://127.0.0.1:8000 to review the simulation. For a physical mission test on the Pi, keep GPIO22 grounded and use actual measured calibration. With the receive decoder prepared in another terminal:

```bash
systemd-run --user --unit=herc-bench --collect --working-directory="$PWD" -p Type=notify -p NotifyAccess=main -p WatchdogSec=15s -p TimeoutStartSec=90s -p TimeoutStopSec=2s -p Restart=no /usr/bin/python3 -m tools.bench --config config.json --source-callsign BENCH --source-ssid 0 --confirm-motion
journalctl --user -u herc-bench -f
```

BENCH and SSID 0 are test fixtures, not NASA sender values. Replace them with the test sender. Wait for BENCH KISS connected before supplying a packet. To test recorded audio through the decoder, use a 24 kHz mono 16-bit WAV generated by Dire Wolf's gen_packets utility:

```bash
python3 -m tools.wav_to_pcm test.wav --startup-delay 3 | direwolf -c deploy/direwolf.conf -r 24000 -t 0 -
```

This checks the decoder-to-mission path, not antenna reception. Also test the actual receive-only SDR chain under rover electrical noise. Stop a bench service with systemctl --user stop herc-bench; closing the log terminal does not stop it. Results are marked BENCH in bench_runs. Remove the jumper and restart the hardware check to test CH6 low-to-high arming, boot-high rejection, transmitter loss and disconnected input separately.

## Qualify and run

Perform the report's physical tests and enter their retained evidence identifiers in a copy of acceptance.template.json. Include OE boot/kill behavior and receiver failsafe measurements. Hash records do not prove that physical tests took place.

```bash
python3 -m tools.commission --config config.json --records acceptance.json --out commissioning.json
python3 -m tools.services
```

The services tool prints the installation commands for this folder. Regenerate services if the folder or results directory changes. Source/configuration/calibration changes require requalification and a new commissioning record. Set a unique excursion ID and trusted clock record in the pit. Receiver and viewer can restart; the task controller does not retry automatically.

Boot the Pi before the excursion. At the task station, stop driving and use the organizer-approved CH6 low-to-high listening initiation. The input must remain low for at least 300 ms, then high for 300 ms; a switch held high at startup cannot arm. Keep permission high through execution. The program accepts a fresh validated packet, executes once and saves the results. There is no preloaded NASA sequence.

## Output disable and limits

OE remains high through startup and arming. The first valid pulse is written before OE is lowered. Completion, an error or receiver disarm raises OE. The software treats CH6 pulses as stale after 250 ms, requires recent main-path progress within 15 seconds and limits execution to 180 seconds. These are software thresholds; Linux scheduling and I/O latency mean they are not measured stop-time guarantees.

An in-process monitor can disable OE while camera work is blocked if the scheduler and GPIO service still operate. Systemd terminates a stalled mission; released GPIO should let the external pull-up disable outputs. Measure normal exit, SIGTERM, SIGKILL, suspended process, reboot, I2C fault and receiver-loss behavior on the actual Pi. No independent microcontroller watchdog remains. A complete Pi/kernel freeze can leave OE low and the PCA9685 repeating its last pulse. Retain an independent accessible servo-power cutoff and guards; add and qualify a separate hardware timeout if bounded stopping during a total Pi failure is required. Suppressing pulses neither removes servo power nor guarantees a mechanical brake.

The autonomous module reserves its own pins and driver. Other rover workloads must pass a simultaneous 60-minute CPU, camera, radio, display and power test. Propulsion control still requires its own reviewed driver interface, speed limit and independent emergency stop; none is provided by this camera-task package.

## Results and verification status

The local viewer at http://127.0.0.1:8000 serves saved original/result images, timestamps and ranging evidence. It has no actuator-control endpoint. Keep KISS and viewer ports private; network access is optional for viewing, not task execution. Use the onboard HDMI display for PER.

Revision 5 passed 93 host tests without failures or skips, including 20 new Pi state/GPIO/PWM tests. The hardware tests use substituted devices and clocks. They do not establish timing, electrical compatibility, RF reception or vision accuracy on the rover. Pi 5 deployment and the full physical acceptance procedure remain required. The previous microcontroller firmware and serial tests are not part of this release.
