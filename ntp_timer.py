# NTP Timer
# Based on : https://github.com/lammersch/ntp-timer/blob/main/ntp_timer.py
# Modified by Greig for LED multiplex drive.


# Imports
import time 
import utime
import network # for Wifi
from machine import Pin # for LED

# using https://mpython.readthedocs.io/en/master/library/micropython/ntptime.html
import ntptime

# to not show Wifi credentials in this code
#from secrets import secrets

# Led for blinking
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

#//////////////////////////////////
#//    Declare some constants    //
#//////////////////////////////////


UTC_OFFSET = 10 * 60 * 60 # in seconds

display_seconds = True	# 'display' will write to six segments when true, four otherwise
display_12hr	= True	# 12 or 24 hour clock mode. Twelve hour mode implies leading hour zero digit suppression.
blank = False			# Future - manually blank the display

# Make naming more convenient
# See: https://docs.python.org/3/library/time.html#time.struct_time
tm_year = 0
tm_mon  = 1 # range [1, 12]
tm_mday = 2 # range [1, 31]
tm_hour = 3 # range [0, 23]
tm_min  = 4 # range [0, 59]
tm_sec  = 5 # range [0, 61] in strftime() description
tm_wday = 6 # range [0, 6] Monday = 0
tm_yday = 7 # range [0, 366]
tm_isdst = 8 # 0, 1 or -1 

# Global flags — must be declared global inside functions before assigning
time_is_set = False
wifi_is_connected = False


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
import os
logfile = open('log.txt', 'a')
# duplicate stdout and stderr to the log file
os.dupterm(logfile)

'''
   wifi_connect() function. Called by set_time()
   Parameters: None
   Return: None
'''
def wifi_connect():
    global wifi_is_connected  # FIX: declare global so the assignment persists

    # Load login data from different file for safety reasons
    ssid = 'ssid' # secrets['ssid']
    password = 'pwd' # secrets['pw']

    # Connect to WiFi
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.connect(ssid, password)

    max_wait = 10
    while max_wait > 0:
        if wlan.status() < 0 or wlan.status() >= 3:
            break
        max_wait -= 1
        print('waiting for connection...')
        time.sleep(5)

    if wlan.status() != 3:
        raise RuntimeError('network connection failed')
    else:
        print('connected')
        wifi_is_connected = True  # FIX: now actually updates the global
        status = wlan.ifconfig()
        print( 'ip = ' + status[0] )
    
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
    year = time.localtime()[0]       #get current year
    HHApril   = time.mktime((year,4 ,30-(int(5*year/4)+4)%7,3,0,0,0,0,0)) # In April drop back to AEST
    HHOctober = time.mktime((year,10,(31-(int(5*year/4+1))%7),2,0,0,0,0,0)) # Advance to DST in October 
    now=time.time()
    
    s = time.localtime(HHApril)
    print(f'HHApril = {HHApril}, {s}')
    s = time.localtime(HHOctober)
    print(f'HHOctober = {HHOctober}, {s}')
    
    if now < HHApril :               # we are before first sunday in April (AEDT)
        cet=time.localtime(now+39600) # AEDT:  UTC+11
        print("we are on Summer time: Jan - April")
    elif now < HHOctober :           # we are before last sunday in october (Winter time)
        cet=time.localtime(now+36000) # AEST:  UTC+10
        print("we are on Winter time")
    else:                            # we are after last sunday of october
        cet=time.localtime(now+39600) # AEDT:  UTC+11
        print("we are on Summer time: October - December")
    return(cet)

'''
   set_time() function. Called by main()
   Parameters: None
   Return: None
'''
def set_time():
    global time_is_set  # FIX: declare global so the assignment persists

    if not wifi_is_connected:
        print("Wifi is not connected, connecting")
        wifi_connect()
    print("UTC time synchronization：%s" %str(time.localtime()))
    
    ntptime.host = "au.pool.ntp.org"
    
    try:
        ntptime.settime()
    except OSError as exc:
        if exc.args[0] == 110: # ETIMEDOUT
            print("ETIMEDOUT. Returning False")
            time.sleep(5)
        
    print("UTC after NTP sync：%s" %str(time.localtime()))
    t = cet_time()
    print("Local time : %s" %str(t))
    
    # Set local clock to adjusted time
    machine.RTC().datetime((t[tm_year], t[tm_mon], t[tm_mday], t[tm_wday] + 1, t[tm_hour], t[tm_min], t[tm_sec], 0))
    print("Local time after synchronization：%s" %str(time.localtime()))
    time_is_set = True  # FIX: now actually updates the global

'''
    schedule() function
    Parameters: t
    Return: none
'''
def schedule(t):
    global time_is_set  # FIX: declare global so the assignment persists

    # Sync clock every day at 03:30
    if t[tm_hour] == 3 and t[tm_min] == 30 and t[tm_sec] == 0:
        time_is_set = False
        print("Synchronizing time")
        set_time()


'''
Builds the list of digit values to display from the current time tuple,
then strobes each cathode in turn.

Called in a tight loop from main() so the display is continuously refreshed.
Each digit gets 2 ms on-time; 6 digits = ~12 ms per full refresh (~83 Hz).
'''
def refresh_display(t):

    _, _, _, HH, MM, SS, _, _ = t

    # Build the six digit values
    if display_12hr:
        if HH == 0:
            HH = 12          # midnight → 12
        elif HH > 12:
            HH -= 12
        digits = [
            10 if HH < 10 else HH // 10,   # blank leading zero in 12-hr mode
            HH % 10,
        ]
    else:
        digits = [HH // 10, HH % 10]

    digits += [MM // 10, MM % 10, SS // 10, SS % 10]

    # Strobe each digit
    num_digits = 6 if display_seconds else 4
    for i in range(num_digits):
        val = digits[i]
        # Set all anode (segment) pins from the chartable bitmap
        for x in range(7):
            anode[x].value(chartable[int(val)] & (1 << x))
        # Enable this digit's cathode briefly, then turn it off
        cathode[i].on()
        time.sleep_ms(2)
        cathode[i].off()


#//////////////////////////////////
#//            MAIN             //
#//////////////////////////////////

def main():
    if not time_is_set:
        set_time()
        
    t = time.localtime()
    o_sec = t[tm_sec]

    while True:
        # FIX: refresh_display() is called every loop iteration so the display
        # is continuously strobed (~83 Hz). Previously it was only called once
        # per second which gave a duty cycle too low to see clearly.
        if not blank:
            refresh_display(t)

        # time.localtime() is fast — sample it every pass
        t = time.localtime()

        # Once per second: blink the onboard LED and run the scheduler
        if o_sec != t[tm_sec]:
            o_sec = t[tm_sec]
            led.toggle()
            schedule(t)


if __name__ == '__main__':
    main()
