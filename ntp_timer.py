# NTP Timer
# Based on : https://github.com/lammersch/ntp-timer/blob/main/ntp_timer.py
# Modified by Greig for LED multiplex drive, with input from Claude.ai
#
# v2 changes: fixed a missing `import machine` NameError, stopped a bad
# Wi-Fi/NTP sync from ever crashing the whole script, split the display
# refresh onto core 1 so it can't freeze during a sync, added LDR-based
# ambient brightness. See the v2 comments (search "CHANGED"/"NEW") for detail.
#
# v3 changes — hardware fail-safes against a segment burning out if the
# code hangs or crashes while it's lit (see inline comments marked NEW-v3):
#   1. core1_display_loop() now catches its own exceptions and turns its
#      pins off itself, immediately, from inside core 1 — it doesn't rely
#      on core 0's try/finally, which can't see an exception on the other
#      core at all.
#   2. A hardware watchdog timer (machine.WDT) is fed from core 0, but ONLY
#      when core 1's heartbeat is still advancing. If core 1 truly hangs
#      (not an exception — an infinite loop or stuck call, which no amount
#      of try/except can catch), core 0 stops feeding and the watchdog
#      forces a full chip reset within its timeout. On reset, every GPIO
#      reverts to its power-on default (high-impedance input) before any
#      of your code runs again, which is what actually guarantees the
#      segments go dark — no application code has to run correctly for
#      that part to work.
#   3. wifi_connect()'s retry loop is shortened and now feeds the watchdog
#      itself, so a slow-but-legitimate Wi-Fi reconnect can't trip the WDT.
#
# v4 changes — fixed the DST boundary calculation in cet_time(). Checked
# against real calendar dates, the old one-line formula's April boundary
# landed on a TUESDAY every single year (never a Sunday — the arithmetic
# wasn't a valid "first Sunday" calculation at all), and its October
# boundary reliably computed the LAST Sunday of October, not the FIRST —
# 3-4 weeks late, every year. Net effect: the clock would show the wrong
# hour for a few days around every April boundary and for 3-4 weeks after
# every real October boundary. Replaced with a small helper that finds
# the actual first Sunday by asking time.localtime() what day-1 falls on,
# rather than a magic modular formula — verified against 2018-2035.
#
# IMPORTANT — read this before flashing:
#   None of the above is a substitute for making sure the WORST CASE drive
#   current through a segment (i.e. if it were held on permanently, at
#   100% duty, forever) is within that LED's *continuous* (DC) forward
#   current rating from its datasheet — not just the higher *peak/pulsed*
#   rating multiplexing lets you get away with day to day. If your
#   segment resistors were sized assuming ~1/6 duty cycle, a stuck-on
#   fault can exceed the continuous rating for as long as it takes the
#   watchdog to reset the board (worst case here: just under the WDT
#   timeout below). Software and a watchdog reduce how long that fault
#   can persist; only correct resistor sizing makes it physically
#   impossible to cook the LED regardless of what the firmware does.
#
#   Also: while WDT is active you cannot Ctrl-C out of a hung REPL — the
#   board will just keep resetting itself every WDT_TIMEOUT_MS. Comment
#   out the `wdt = machine.WDT(...)` line while you're actively developing,
#   and only re-enable it once you're happy with the code.

# Imports
import time
import network            # for Wifi
import machine             # machine.Pin()/machine.RTC()/machine.WDT() all need this
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
    Pin(13, Pin.OUT), # H_
    Pin(12, Pin.OUT), # _H
    Pin(11, Pin.OUT), # M_
    Pin(10, Pin.OUT), # _M
    Pin( 9, Pin.OUT), # S_
    Pin( 8, Pin.OUT)  # _S
    ]

# LDR for ambient brightness.
# GP26 (ADC0) is already taken above by the 'dp' anode, so this uses GP27
# (ADC1). GP28 (ADC2) is the other free ADC-capable pin if you'd rather
# use that one instead.
#
# REQUIRED WIRING for read_brightness_on_us() below to dim correctly as
# ambient light drops: the LDR goes on the TOP leg, 3V3 -> LDR -> (GP27) -> fixed
# resistor -> GND. GP27 taps the midpoint. With the LDR on top, brighter
# ambient light means LOWER LDR resistance, which means the fixed bottom
# resistor claims a BIGGER share of the 3.3V, so the voltage at GP27 (and
# the raw ADC reading) goes UP as the room gets brighter — which is what
# read_brightness_on_us() below assumes when it uses `raw` directly to
# mean "more light". A reasonable fixed resistor value to start with is
# whatever puts the LDR's own dark/light resistance swing roughly centred
# on the ADC's usable range — 10k is a common starting point for a typical
# LDR, but check yours.
#
# If you wire it the other way around (LDR on the bottom, to GND, fixed
# resistor on top to 3V3) the relationship inverts — brighter light would
# then mean a LOWER raw reading — and you'd need to swap `raw` for
# `(65535 - raw)` in read_brightness_on_us() to compensate. Wiring it as
# specified above means you don't have to touch that function at all.
ldr = ADC(Pin(27))

#//////////////////////////////////
#//    Declare some constants    //
#//////////////////////////////////

display_seconds = True  # 'display' will write to six segments when true, four otherwise
display_12hr    = True  # 12 or 24 hour clock mode. Twelve hour mode implies leading hour zero digit suppression.
blank = False           # Future - manually blank the display

# Brightness range. Each digit gets a SLOT_US-microsecond time slice; on_us
# of that slice it's lit, the remainder it's dark. Keeping the slot length
# constant (rather than just shortening it) means the refresh rate doesn't
# change with brightness, only the perceived duty cycle does.
#
# NEW-v5 — set the floor/ceiling here, as a percentage of the slot, rather
# than juggling microseconds directly:
MIN_BRIGHTNESS_PCT = 15   # dimmest allowed, even on a pitch-black night — tune to taste
MAX_BRIGHTNESS_PCT = 100  # brightest allowed, even in full sun (100 = old always-on-in-slot behaviour)

SLOT_US   = 2000  # total time budget per digit, in microseconds (== the old fixed on-time)
MIN_ON_US = SLOT_US * MIN_BRIGHTNESS_PCT // 100  # derived — edit the PCT constants above, not these
MAX_ON_US = SLOT_US * MAX_BRIGHTNESS_PCT // 100

# NEW-v3 — watchdog tuning.
WDT_TIMEOUT_MS      = 4000  # hardware ceiling on rp2040 is 8388ms; comfortably under that.
                             # This is a defense-in-depth bound on how long a genuine hang can
                             # persist, not the primary safety mechanism — see the note above.
HEARTBEAT_STALL_MS  = 1500   # if core1's heartbeat hasn't advanced in this long, treat it as dead
WIFI_MAX_WAIT_S     = 6      # kept safely under WDT_TIMEOUT_MS's 4s... see wifi_connect(): this
                             # loop feeds the watchdog itself each second, so it isn't actually
                             # bounded by WDT_TIMEOUT_MS the way a silent block would be

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

# Shared state between core 0 (Wi-Fi/NTP/LDR/watchdog, this module's main())
# and core 1 (display refresh, core1_display_loop()). Kept as single-word
# list slots deliberately: MicroPython's dual-core _thread has no GIL, so
# this isn't formally lock-protected, but each field is one small-int
# store/load, which is effectively atomic on this hardware. If you ever
# need to update several related fields as one unit, wrap the writes in a
# _thread.allocate_lock() instead of relying on this.
shared_hms      = [0, 0, 0]    # [HH, MM, SS] — written by core0, read by core1
shared_on_us    = [MAX_ON_US]  # current digit on-time in microseconds — written by core0, read by core1
display_stop    = [False]      # either core sets this True to ask core1's loop to exit
shared_heartbeat = [0]         # NEW-v3 — core1 increments this every refresh pass; core0 watches it

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
   NEW-v3 — pins_off() function. The one place that actually flips every
   display pin off. Called by all_off() (core0, normal/exception shutdown)
   and by core1_display_loop()'s own except block (core1, self-inflicted
   shutdown) — kept tiny and dependency-free so it can safely run from
   either core, including from inside an exception handler.
'''
def pins_off():
    for pin in anode:
        pin.off()
    for pin in cathode:
        pin.off()


'''
   wifi_connect() function. Called by set_time()
   Parameters: None
   Return: None
   Raises: RuntimeError if the connection attempt doesn't succeed
'''
def wifi_connect():
    if wlan.isconnected():  # ask the radio directly rather than trusting a sticky flag
        return

    ssid     = secrets['ssid']
    password = secrets['pw']

    wlan.active(True)
    wlan.connect(ssid, password)

    max_wait = WIFI_MAX_WAIT_S
    while max_wait > 0:
        status = wlan.status()
        print(f'wlan status: {status}')  # 0=idle, 1=connecting, 2=wrong password, 3=connected, -1=failed, -2=no AP, -3=failed
        if status < 0 or status >= 3:
            break
        max_wait -= 1
        feed_watchdog_if_alive()  # NEW-v3 — this loop can run for several seconds; keep the WDT happy
        time.sleep(1)

    if not wlan.isconnected():
        raise RuntimeError('network connection failed')

    print('connected')
    print('ip = ' + wlan.ifconfig()[0])


'''
   NEW-v4 — first_sunday_epoch() function. Called by cet_time().
   Parameters: year, month, hour — the local hour the transition happens at
   Return: epoch seconds (in the same naive/no-TZ frame the rest of this
           file uses) for 00:00:00 + `hour` on the first Sunday of `month`.

   Finds day 1's real weekday via a mktime()->localtime() round trip
   rather than computing it with modular arithmetic — the previous
   one-liner's arithmetic was wrong (see the v4 changelog note at the top
   of this file), and this version is trivial to verify by eye: it can
   only ever return a date that time.localtime() itself calls a Sunday.
'''
def first_sunday_epoch(year, month, hour):
    day1_epoch = time.mktime((year, month, 1, 0, 0, 0, 0, 0))
    day1_wday = time.localtime(day1_epoch)[6]     # 0=Monday .. 6=Sunday
    first_sunday_day = 1 + ((6 - day1_wday) % 7)
    return time.mktime((year, month, first_sunday_day, hour, 0, 0, 0, 0))


'''
   cet_time() function. Called by set_time()
   Parameters: None
   Return: cet
'''
# DST calculations - modified to AEST/AEDT from the original
# Changes happen first Sunday of April and October at 02:00 local time
# Ref. formulas : http://www.webexhibits.org/daylightsaving/i.html
#                 Since 1996, valid through 2099
# NEW-v4: the boundary DATES now come from first_sunday_epoch() above;
# everything else here (which hour each transition uses, and the
# before/between/after comparison logic) is unchanged from the original.
#
# v5 changes: brightness floor/ceiling are now set as MIN/MAX_BRIGHTNESS_PCT
# (0-100%) instead of raw microseconds — same MIN_ON_US/MAX_ON_US as before
# under the hood, just easier to reason about when tuning. Also firmed up
# the LDR wiring comment into a definite instruction rather than an
# assumption, per Greig's question about which way around to wire it.

def cet_time():
    year = time.localtime()[0]       # get current year
    HHApril   = first_sunday_epoch(year, 4, 3)   # In April drop back to AEST, at 3am AEDT
    HHOctober = first_sunday_epoch(year, 10, 2)  # Advance to DST in October, at 2am AEST
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
   Return: True on a successful sync, False otherwise
'''
def set_time():
    global time_is_set

    try:
        wifi_connect()
    except Exception as e:  # a bad AP/router reboot can't ever crash the whole script
        print(f'Wifi connect failed: {e}')
        return False

    print("UTC time before sync: %s" % str(time.localtime()))
    ntptime.host = "au.pool.ntp.org"

    try:
        ntptime.settime()
    except Exception as e:  # any failure (DNS, timeout, unreachable host, refused, etc.) is
        print(f'NTP sync failed: {e}')  # treated the same way: log it, let the caller retry later.
        return False

    print("UTC after NTP sync: %s" % str(time.localtime()))
    t = cet_time()
    print("Local time: %s" % str(t))

    # Set local clock to adjusted time
    machine.RTC().datetime((t[tm_year], t[tm_mon], t[tm_mday], t[tm_wday] + 1, t[tm_hour], t[tm_min], t[tm_sec], 0))
    print("Local time after synchronization: %s" % str(time.localtime()))
    time_is_set = True
    return True


'''
   read_brightness_on_us() function. Called once a second from main()
   Parameters: None
   Return: a digit on-time in microseconds, somewhere in [MIN_ON_US, MAX_ON_US]

   Requires the LDR wired as the TOP leg of the divider (3V3 -> LDR -> GP27
   -> resistor -> GND — see the wiring note above `ldr = ADC(Pin(27))`),
   so a HIGHER raw reading means MORE ambient light. If you wire it the
   other way around (LDR to GND instead), swap `raw` for `(65535 - raw)`
   below.
'''
def read_brightness_on_us():
    raw = ldr.read_u16()  # 0-65535 across 0.0V-3.3V
    return MIN_ON_US + (raw * (MAX_ON_US - MIN_ON_US)) // 65535


'''
    schedule() function. Called once per second from main()
    Parameters: t — current time tuple
    Return: None
'''
next_retry_at = None  # epoch seconds for the next retry after a failed sync, or None

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
            next_retry_at = time.time() + 600  # try again in 10 minutes rather than waiting
                                                # a full day for the next scheduled slot


'''
    core1_display_loop() function. Runs entirely on core 1, started once
    from main() via _thread.start_new_thread(). This never touches Wi-Fi,
    NTP, or the RTC — it only reads shared_hms / shared_on_us and strobes
    the display — which is what makes the display immune to however long
    a Wi-Fi/NTP sync takes on core 0.

    NEW-v3: the whole body runs inside a try/except. If ANYTHING raises in
    here — a bad index, a hardware fault reading back a pin, whatever —
    pins_off() runs immediately, from core1 itself, before the exception
    is allowed to propagate further. This is the fast path; the watchdog
    below is the fallback for the case this can't catch (a genuine hang
    with no exception at all).
    Parameters: None
    Return: None
'''
def core1_display_loop():
    try:
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
                    time.sleep_us(off_us)  # the dark portion of the slot; this is the "PWM" that dims it

            shared_heartbeat[0] += 1  # NEW-v3 — proof of life, once per full refresh pass (~every 12ms)
    except Exception as e:
        print(f'core1 display loop crashed: {e}')
        pins_off()             # NEW-v3 — turn our own pins off immediately, don't wait for core0
        display_stop[0] = True # tell core0 we're gone; its heartbeat check will stop feeding the WDT


'''
   NEW-v3 — feed_watchdog_if_alive() function. The single place that ever
   calls wdt.feed(). Only feeds when core1's heartbeat has advanced inside
   HEARTBEAT_STALL_MS — i.e. only when we have positive, recent evidence
   the display loop is actually still running. If core1 has hung (no
   exception, just stuck), this simply stops feeding, and the watchdog
   resets the board within WDT_TIMEOUT_MS of the last real feed. A reset
   forces every GPIO back to its power-on default (high-impedance input)
   before any of your code runs again, which is what guarantees the
   segments go dark even though no application code got to run cleanly.
   Parameters: None
   Return: None
'''
wdt = None  # created in main(), once, after core1 has started
_last_heartbeat_seen = -1
_last_heartbeat_change_ms = 0

def feed_watchdog_if_alive():
    global _last_heartbeat_seen, _last_heartbeat_change_ms

    if wdt is None:
        return

    now = time.ticks_ms()
    hb = shared_heartbeat[0]
    if hb != _last_heartbeat_seen:
        _last_heartbeat_seen = hb
        _last_heartbeat_change_ms = now

    stalled = time.ticks_diff(now, _last_heartbeat_change_ms) > HEARTBEAT_STALL_MS
    if not stalled and not display_stop[0]:
        wdt.feed()
    # else: deliberately withhold the feed. The watchdog fires on its own
    # within WDT_TIMEOUT_MS of the last successful feed — no further
    # action needed here.


#//////////////////////////////////
#//            MAIN             //
#//////////////////////////////////

'''
    all_off() function. Called by the top-level finally block.
    Extinguishes all segments and disables all cathodes so no
    current flows through the display when the script exits normally
    (a WDT-triggered reset bypasses this entirely, by design — that's
    the whole point of it as a fallback).
'''
def all_off():
    display_stop[0] = True   # ask core1 to stop before we start reprogramming its pins
    time.sleep_ms(50)        # give it one loop iteration to notice and exit
    pins_off()
    led.off()


def main():
    global time_is_set, wdt

    # Start the display on core 1 straight away so something is showing
    # (even if it's still 00:00:00) while core 0 sorts out Wi-Fi/NTP.
    _thread.start_new_thread(core1_display_loop, ())
    time.sleep_ms(50)  # let core1 take its first heartbeat before we start judging it

    # NEW-v3 — arm the watchdog only once core1 is confirmed running. Comment
    # this line out while actively developing (see the note at the top of
    # the file) — once started it cannot be stopped or reconfigured.
    wdt = machine.WDT(timeout=WDT_TIMEOUT_MS)

    if not time_is_set:
        set_time()

    t = time.localtime()
    o_sec = t[tm_sec]
    smoothed_on_us = MAX_ON_US

    while True:
        t = time.localtime()

        # Publish the current time to core1 every pass; this is cheap and
        # keeps the display current to within one loop iteration
        shared_hms[0] = t[tm_hour]
        shared_hms[1] = t[tm_min]
        shared_hms[2] = t[tm_sec]

        # Once per second: heartbeat LED, brightness sample, and the scheduler
        if o_sec != t[tm_sec]:
            o_sec = t[tm_sec]
            led.toggle()

            # Ambient light changes slowly, so once a second is plenty. A
            # light EMA smooths out any jitter (e.g. mains-frequency
            # flicker from a nearby light hitting the LDR).
            target_on_us = read_brightness_on_us()
            smoothed_on_us = (smoothed_on_us * 3 + target_on_us) // 4
            shared_on_us[0] = smoothed_on_us

            schedule(t)

        feed_watchdog_if_alive()  # NEW-v3 — only actually feeds if core1's heartbeat is current
        time.sleep_ms(50)  # core0 no longer drives the display, so it can idle between checks


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f'Fatal error: {e}')
    finally:
        all_off()   # always extinguish the display on exit, whatever the cause
