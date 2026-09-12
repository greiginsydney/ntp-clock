# NTP Timer
# Based on : https://github.com/lammersch/ntp-timer/blob/main/ntp_timer.py
# Modified by Greig for LED multiplex drive, with input from Claude.ai
#
# v2 changes (see inline comments marked CHANGED / NEW for the detail):
#   1. Fixed a NameError bug: `machine.Pin(...)` / `machine.RTC()` were used
#      but only `from machine import Pin` had been imported — `machine`
#      itself was never imported, so the original script would crash before
#      even reaching main().
#   2. Fixed a crash-on-Wi-Fi-hiccup bug: any exception other than a bare
#      NTP timeout (errno 110) during the nightly resync was uncaught, so it
#      propagated out of main(), hit the top-level except, called all_off(),
#      and left the clock permanently blank until manually restarted. Sync
#      failures are now caught broadly and just retried in 10 minutes.
#   3. The display multiplex loop now runs on core 1, completely decoupled
#      from Wi-Fi/NTP on core 0 — this is what actually stops the display
#      freezing during a sync, rather than just making the freeze shorter.
#   4. Added LDR-based ambient brightness via software PWM of each digit's
#      on-time within its multiplex slot.

# Imports
import time
import network            # for Wifi
import machine             # NEW — see note #1 above; machine.Pin()/machine.RTC() need this
from machine import Pin, ADC
import _thread
import os

# using https://mpython.readthedocs.io/en/master/library/micropython/ntptime.html
import ntptime

# Wifi credentials are kept in a separate file — do not commit secrets.py to source control
from secrets import secrets

# Onboard LED, used as a once-a-second heartbeat
led = machine.Pin("LED", machine.Pin.OUT)

anode = [
    Pin(16, Pin.OUT), # a
    Pin(17, Pin.OUT), # b
    Pin(18, Pin.OUT), # c
    Pin(19, Pin.OUT), # d
    Pin(20, Pin.OUT), # e
    Pin(21, Pin.OUT), # f
    Pin(22, Pin.OUT), # g
    Pin(26, Pin.OUT)  # dp
    ]

cathode = [
    Pin( 8, Pin.OUT), # H_
    Pin( 9, Pin.OUT), # _H
    Pin(10, Pin.OUT), # M_
    Pin(11, Pin.OUT), # _M
    Pin(12, Pin.OUT), # S_
    Pin(13, Pin.OUT)  # _S
    ]

# NEW — LDR for ambient brightness.
# GP26 (ADC0) is already taken above by the 'dp' anode, so this uses GP27
# (ADC1). GP28 (ADC2) is the other free ADC-capable pin if you'd rather
# use that one instead.
ldr = ADC(Pin(27))

#//////////////////////////////////
#//    Declare some constants    //
#//////////////////////////////////

display_seconds = True  # 'display' will write to six segments when true, four otherwise
display_12hr    = True  # 12 or 24 hour clock mode. Twelve hour mode implies leading hour zero digit suppression.
blank = False           # Future - manually blank the display

# NEW — brightness range. Each digit gets a SLOT_US-microsecond time slice;
# on_us of that slice it's lit, the remainder it's dark. Keeping the slot
# length constant (rather than just shortening it) means the refresh rate
# doesn't change with brightness, only the perceived duty cycle does.
SLOT_US  = 2000   # total time budget per digit, in microseconds (== the old fixed on-time)
MIN_ON_US = 300    # dimmest setting — tune to taste, some visible light at the low end
MAX_ON_US = SLOT_US  # brightest setting — full slot on, same as the original behaviour

# Make naming more convenient
# See: https://docs.python.org/3/library/time.html#time.struct_time
tm_year  = 0
tm_mon   = 1 # range [1, 12]
tm_mday  = 2 # range [1, 31]
tm_hour  = 3 # range [0, 23]
tm_min   = 4 # range [0, 59]
tm_sec   = 5 # range [0, 61] in strftime() description
tm_wday  = 6 # range [0, 6] Monday = 0
tm_yday  = 7 # range [0, 366]
tm_isdst = 8 # 0, 1 or -1

# Global flags — must be declared global inside functions before assigning
time_is_set = False

# NEW — shared state between core 0 (Wi-Fi/NTP/LDR, this module's main()) and
# core 1 (display refresh, core1_display_loop()). Kept as single-word list
# slots deliberately: MicroPython's dual-core _thread has no GIL, so this
# isn't formally lock-protected, but each field is one small-int store/load,
# which is effectively atomic on this hardware — worst case core1 uses one
# stale value for a fraction of a 2ms slot, which is imperceptible. If you
# ever need to update several related fields as one unit, wrap the writes
# in a _thread.allocate_lock() instead of relying on this.
shared_hms   = [0, 0, 0]       # [HH, MM, SS] — written by core0, read by core1
shared_on_us = [MAX_ON_US]     # current digit on-time in microseconds — written by core0, read by core1
display_stop = [False]         # core0 sets this True to ask core1's loop to exit cleanly

wlan = network.WLAN(network.STA_IF)  # module-level so wifi_connect()/set_time() can both query real status

#   Bitmap calc's to directly address the segments:
#      1
#     ----
#    |    |
#    |    | 2
# 32 |    |
#     ----     <-- 64
#    |    |
# 16 |    | 4
#    |    |
#     ----
#      8

chartable = [
  0b00111111, # 0
  0b00000110, # 1
  0b11011011, # 2
  0b11001111, # 3
  0b11100110, # 4
  0b11101101, # 5
  0b11111101, # 6
  0b00000111, # 7
  0b11111111, # 8
  0b11101111, # 9
  0b00000000  # off / blank
]

# Logging
try:
    logfile = open('log.txt', 'a')
    os.dupterm(logfile)
except Exception as e:
    print(f'Warning: could not open log file: {e}')


#//////////////////////////////////
#//         FUNCTIONS           //
#//////////////////////////////////

'''
   wifi_connect() function. Called by set_time()
   Parameters: None
   Return: None
   Raises: RuntimeError if the connection attempt doesn't succeed
'''
def wifi_connect():
    if wlan.isconnected():  # CHANGED — ask the radio directly rather than trusting a sticky flag
        return

    ssid     = secrets['ssid']
    password = secrets['pw']

    wlan.active(True)
    wlan.connect(ssid, password)

    max_wait = 10
    while max_wait > 0:
        status = wlan.status()
        print(f'wlan status: {status}')  # 0=idle, 1=connecting, 2=wrong password, 3=connected, -1=failed, -2=no AP, -3=failed
        if status < 0 or status >= 3:
            break
        max_wait -= 1
        time.sleep(1)

    if not wlan.isconnected():
        raise RuntimeError('network connection failed')

    print('connected')
    print('ip = ' + wlan.ifconfig()[0])


'''
   cet_time() function. Called by set_time()
   Parameters: None
   Return: cet
'''
# DST calculations - modified to AEST/AEDT from the original
# Changes happen first Sunday of April and October at 02:00 local time
# Ref. formulas : http://www.webexhibits.org/daylightsaving/i.html
#                 Since 1996, valid through 2099

def cet_time():
    year = time.localtime()[0]       # get current year
    HHApril   = time.mktime((year,4 ,30-(int(5*year/4)+4)%7,3,0,0,0,0,0)) # In April drop back to AEST
    HHOctober = time.mktime((year,10,(31-(int(5*year/4+1))%7),2,0,0,0,0,0)) # Advance to DST in October
    now = time.time()

    s = time.localtime(HHApril)
    print(f'HHApril = {HHApril}, {s}')
    s = time.localtime(HHOctober)
    print(f'HHOctober = {HHOctober}, {s}')

    if now < HHApril:                   # before first Sunday in April (still AEDT from previous year)
        cet = time.localtime(now+39600) # AEDT: UTC+11
        print("we are on Summer time: Jan - April")
    elif now < HHOctober:               # between April and October (AEST)
        cet = time.localtime(now+36000) # AEST: UTC+10
        print("we are on Winter time")
    else:                               # after first Sunday in October (AEDT)
        cet = time.localtime(now+39600) # AEDT: UTC+11
        print("we are on Summer time: October - December")
    return cet


'''
   set_time() function. Called by main() and schedule()
   Parameters: None
   Return: True on a successful sync, False otherwise — CHANGED, used to be None/exceptions
'''
def set_time():
    global time_is_set

    try:
        wifi_connect()
    except Exception as e:  # CHANGED — was only reachable via the caller; now caught here so a
        print(f'Wifi connect failed: {e}')  # bad AP/router reboot can't ever crash the whole script
        return False

    print("UTC time before sync: %s" % str(time.localtime()))
    ntptime.host = "au.pool.ntp.org"

    try:
        ntptime.settime()
    except Exception as e:  # CHANGED — was `except OSError as exc: if exc.args[0] == 110 ... else raise`.
        print(f'NTP sync failed: {e}')  # Any failure (DNS, timeout, unreachable host, refused, etc.)
        return False          # is now treated the same way: log it and let the caller retry later.

    print("UTC after NTP sync: %s" % str(time.localtime()))
    t = cet_time()
    print("Local time: %s" % str(t))

    # Set local clock to adjusted time
    machine.RTC().datetime((t[tm_year], t[tm_mon], t[tm_mday], t[tm_wday] + 1, t[tm_hour], t[tm_min], t[tm_sec], 0))
    print("Local time after synchronization: %s" % str(time.localtime()))
    time_is_set = True
    return True


'''
   NEW — read_brightness_on_us() function. Called once a second from main()
   Parameters: None
   Return: a digit on-time in microseconds, somewhere in [MIN_ON_US, MAX_ON_US]

   Assumes the LDR is wired as the TOP leg of a divider (3V3 -> LDR -> ADC
   node -> resistor -> GND), so a HIGHER raw reading means MORE ambient
   light. If your wiring is the other way around (LDR to GND instead),
   swap `raw` for `(65535 - raw)` below.
'''
def read_brightness_on_us():
    raw = ldr.read_u16()  # 0-65535 across 0.0V-3.3V
    return MIN_ON_US + (raw * (MAX_ON_US - MIN_ON_US)) // 65535


'''
    schedule() function. Called once per second from main()
    Parameters: t — current time tuple
    Return: None
'''
next_retry_at = None  # NEW — epoch seconds for the next retry after a failed sync, or None

def schedule(t):
    global time_is_set, next_retry_at

    due_now   = (t[tm_hour] == 3 and t[tm_min] == 30 and t[tm_sec] == 0)
    due_retry = (next_retry_at is not None and time.time() >= next_retry_at)

    if due_now or due_retry:
        time_is_set = False
        print("Synchronizing time" if due_now else "Retrying previously-failed sync")
        if set_time():
            next_retry_at = None
        else:
            next_retry_at = time.time() + 600  # NEW — try again in 10 minutes rather than waiting
                                                # a full day for the next scheduled slot


'''
    NEW — core1_display_loop() function. Runs entirely on core 1, started
    once from main() via _thread.start_new_thread(). This never touches
    Wi-Fi, NTP, or the RTC — it only reads shared_hms / shared_on_us and
    strobes the display — which is what actually makes the display
    immune to however long a Wi-Fi/NTP sync takes on core 0.
    Parameters: None
    Return: None
'''
def core1_display_loop():
    while not display_stop[0]:
        if blank:
            time.sleep_ms(50)
            continue

        HH, MM, SS = shared_hms
        on_us = shared_on_us[0]
        off_us = SLOT_US - on_us
        if off_us < 0:
            off_us = 0

        if display_12hr:
            h12 = HH
            if h12 == 0:
                h12 = 12             # midnight -> 12
            elif h12 > 12:
                h12 -= 12
            digits = [
                10 if h12 < 10 else h12 // 10,   # blank leading zero in 12-hr mode
                h12 % 10,
            ]
        else:
            digits = [HH // 10, HH % 10]

        digits += [MM // 10, MM % 10, SS // 10, SS % 10]

        num_digits = 6 if display_seconds else 4
        for i in range(num_digits):
            val = digits[i]
            for x in range(7):
                anode[x].value(chartable[val] & (1 << x))
            cathode[i].on()
            time.sleep_us(on_us)
            cathode[i].off()
            if off_us:
                time.sleep_us(off_us)  # NEW — the dark portion of the slot; this is the "PWM" that dims it


#//////////////////////////////////
#//            MAIN             //
#//////////////////////////////////

'''
    all_off() function. Called by the top-level finally block.
    Extinguishes all segments and disables all cathodes so no
    current flows through the display when the script exits.
'''
def all_off():
    display_stop[0] = True   # NEW — ask core1 to stop before we start reprogramming its pins
    time.sleep_ms(50)        # give it one loop iteration to notice and exit
    for pin in anode:
        pin.off()
    for pin in cathode:
        pin.off()
    led.off()


def main():
    global time_is_set

    # NEW — start the display on core 1 straight away so something is showing
    # (even if it's still 00:00:00) while core 0 sorts out Wi-Fi/NTP.
    _thread.start_new_thread(core1_display_loop, ())

    if not time_is_set:
        set_time()

    t = time.localtime()
    o_sec = t[tm_sec]
    smoothed_on_us = MAX_ON_US

    while True:
        t = time.localtime()

        # NEW — publish the current time to core1 every pass; this is cheap
        # and keeps the display current to within one loop iteration
        shared_hms[0] = t[tm_hour]
        shared_hms[1] = t[tm_min]
        shared_hms[2] = t[tm_sec]

        # Once per second: heartbeat LED, brightness sample, and the scheduler
        if o_sec != t[tm_sec]:
            o_sec = t[tm_sec]
            led.toggle()

            # NEW — ambient light changes slowly, so once a second is plenty.
            # A light EMA smooths out any jitter (e.g. mains-frequency flicker
            # from a nearby light hitting the LDR).
            target_on_us = read_brightness_on_us()
            smoothed_on_us = (smoothed_on_us * 3 + target_on_us) // 4
            shared_on_us[0] = smoothed_on_us

            schedule(t)

        time.sleep_ms(50)  # CHANGED — core0 no longer drives the display, so it can idle between checks


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f'Fatal error: {e}')
    finally:
        all_off()   # always extinguish the display on exit, whatever the cause
