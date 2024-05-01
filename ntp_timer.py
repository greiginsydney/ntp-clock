# NTP Timer
# Based on : https://github.com/lammersch/ntp-timer/blob/main/ntp_timer.py
# Modified by Greig for LED multiplex drive.


# Imports
import time 
import utime
import network # for Wifi
from machine import Pin # for LED
from machine import Timer # for timer-based interrupts

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

'''
constants
'''

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
tm_wday = 6 # range 8[0, 6] Monday = 0
tm_yday = 7 # range [0, 366]
tm_isdst = 8 # 0, 1 or -1 


#   Bitmap calc's to directly address the segments:
#      1
#     ----
#    |    |
# 32 |    | 2
#    |    |
#     ----     <-- 64
#    |    |
# 16 |    | 4
#    |    |
#     ----
#      8

chartable = [
  0b00111111, # 0
  0b00000110,
  0b11011011,
  0b11001111,
  0b11100110,
  0b11101101,
  0b11111101, # 6
  0b00000111,
  0b11111111,
  0b11101111, # 9
  0b00000000  # off
]

'''
variables
'''

t = 0, 0, 0, 0, 0, 0, 0, 0	#The clock will display this on power-up until WiFi connects and we get the time from NTP.

# Global to check if time is set
time_is_set = False
wifi_is_connected = False


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
    # Load login data from different file for safety reasons
    ssid = 'ssid' # secrets['ssid']
    password = 'password' # secrets['pw']

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
            wifi_is_connected = True
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
        cet=time.localtime(now+39600) # CET:  UTC+1H
        print("we are on Summer time: October - December")
    return(cet)

'''
   set_time() function. Called by main()
   Parameters: None
   Return: None
'''
def set_time():
    # ntp_host = ["0.europe.pool.ntp.org", "1.europe.pool.ntp.org", "2.europe.pool.ntp.org", "3.europe.pool.ntp.org"] # Not used
    global t
    
    if not wifi_is_connected:
        print("Wifi is not connected, connecting")
        wifi_connect()
    print("UTC time synchronization：%s" %str(time.localtime()))
    
    # if needed, we'll cycle over ntp-servers here using:
    ntptime.host = "au.pool.ntp.org"
    
    try:
        ntptime.settime()
    except OSError as exc:
        if exc.args[0] == 110: # ETIMEDOUT
            print("ETIMEDOUT. Returning False")
            time.sleep(5)
        
    print("UTC after NTP sync：%s" %str(time.localtime()))
    #t = time.localtime(time.time() + UTC_OFFSET) # Apply UTC offset
    t = cet_time()
    print("Local time : %s" %str(t))
    
    # Set local clock to adjusted time
    machine.RTC().datetime((t[tm_year], t[tm_mon], t[tm_mday], t[tm_wday] + 1, t[tm_hour], t[tm_min], t[tm_sec], 0))
    print("Local time after synchronization：%s" %str(time.localtime()))    
    time_is_set = True
    #WLAN.disconnect()
    #print("Disconnected WiFi")

'''
schedule() function
Parameters: t
Return: none
'''

# Todo: create crontab like structure with: minute / hour / day of month / month / day of week / command
# Todo: check what happens when scheduled tasks overlap (uasyncio)

def schedule(t):
    #if t[tm_hour] == 17 and t[tm_min] == 45 and t[tm_sec] == 00: # Define seconds, or it will run every second...
    #    print("Executing doorOperations")
    #    door_operations.closeDoor()
    
    # Sync clock every day
    if t[tm_hour] == 03 and t[tm_min] == 30 and t[tm_sec] == 0:
        time_is_set = False
        print("Synchronizing time")
        set_time()

'''
Takes the passed time and prepares it in readiness to write to the displays
It then calls 'show_char' 4 (or 6) times.
Segments are the ANODES for this common-cathode display
Cathodes are the common cathode of each character respectively.
'''
def display(junk):
    global t
    
    # display_seconds = True	# 'display' will write to six segments when true, four otherwise
    # display_12hr	= True	# 12 or 24 hour clock mode. Twelve hour mode implies leading hour zero digit suppression.

    # Time 't' is a tuple containing year, month, mday, hour (24 hour format), minute, second, weekday, yearday
    _, _, _, HH, MM, SS, _, _ = t
    
    SS = 4
  
    # Write the hour. If display_12hr = True, convert to 12 hour time and blank the leading digit if it's a zero:
    if display_12hr == True:
        if HH > 12:
            HH -= 12
        if HH < 10:
            show_char(10, 0)	# A value of 10 will blank the display
        else:
            tens_value = int(HH / 10)
            show_char(tens_value, 0)
        units_value = HH % 10
        show_char(units_value, 1)
    else:
        tens_value = int(HH / 10)
        show_char(tens_value, 0)
        units_value = HH % 10    
        show_char(units_value, 1)
    
    count = 2
    for value in MM, SS:
        tens_value = int(value / 10)
        show_char(tens_value, count)
        count += 1
        units_value = value % 10    
        show_char(units_value, count)
        count += 1
        
        
'''
Takes a given single-digit decimal and writes that to the nominated display
A 'value' of -1 is the signal to blank the character
'''
def show_char(value, display_number):

    if (display_number) == 0:
        cathode[5].off()
    else:
        cathode[display_number - 1].off()
    
    #for y in range (0,5):
    #    cathode[y].off()
    for x in range (0,7):
        greig = chartable[int(value)]  & (0b00000001 << x)
        anode[x].value(greig) # A 1 turns the GPIO pin on/high and a 0 is off/low
        time.sleep_ms(100)

    cathode[display_number].on()
    time.sleep_ms(2)
    

'''
Instantiate timer & ISR
'''

# Instantiate the IRQ timer
timer = Timer()

# Attach the interrupt
timer.init(mode=Timer.PERIODIC, freq=1000, callback=display)


'''
MAIN
'''
def main():
    global t
    
    if not time_is_set:
        set_time()
        
    t = time.localtime()
    o_sec = time.localtime()[5]

    while True:
        # if not blank:
        #    display(t)		# Write the time to the LEDs
        if  o_sec != t[5]:		# Every second, toggle the on-board LED & check the schedule
            t = time.localtime()	# read the current time
            o_sec = t[5]
            led.on()
            schedule(t)
            # print(t)
            led.off()


if __name__ == '__main__':
    main()

