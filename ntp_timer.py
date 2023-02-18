# NTP Timer

# Imports
import time 
import utime
import network # for Wifi
from machine import Pin # for LED

# using https://mpython.readthedocs.io/en/master/library/micropython/ntptime.html
import ntptime

# to not show Wifi credentials in this code
from secrets import secrets

# Led for blinking
led = machine.Pin("LED", machine.Pin.OUT)


# Constants
UTC_OFFSET = 2 * 60 * 60 # in seconds
#UTC_OFFSET = 0 # in seconds

# Make naming more convenient
# See: https://docs.python.org/3/library/time.html#time.struct_time
tm_year = 0
tm_mon = 1 # range [1, 12]
tm_mday = 2 # range [1, 31]
tm_hour = 3 # range [0, 23]
tm_min = 4 # range [0, 59]
tm_sec = 5 # range [0, 61] in strftime() description
tm_wday = 6 # range 8[0, 6] Monday = 0
tm_yday = 7 # range [0, 366]
tm_isdst = 8 # 0, 1 or -1 

# Global to check if time is set
time_is_set = False
wifi_is_connected = False

# Logging
import os
logfile = open('log.txt', 'a')
# duplicate stdout and stderr to the log file
os.dupterm(logfile)

"""
   wifi_connect() function. Called by set_time()
   Parameters: None
   Return: None
"""
def wifi_connect():
    # Load login data from different file for safety reasons
    ssid = secrets['ssid']
    password = secrets['pw']

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
    
"""
   cet_time() function. Called by set_time()
   Parameters: None
   Return: cet
"""
# DST calculations
# This code returns the Central European Time (CET) including daylight saving
# Winter (CET) is UTC+1H Summer (CEST) is UTC+2H
# Changes happen last Sundays of March (CEST) and October (CET) at 01:00 UTC
# Ref. formulas : http://www.webexhibits.org/daylightsaving/i.html
#                 Since 1996, valid through 2099


def cet_time():
    year = time.localtime()[0]       #get current year
    HHMarch   = time.mktime((year,3 ,(31-(int(5*year/4+4))%7),1,0,0,0,0,0)) #Time of March change to CEST
    HHOctober = time.mktime((year,10,(31-(int(5*year/4+1))%7),1,0,0,0,0,0)) #Time of October change to CET
    now=time.time()
    if now < HHMarch :               # we are before last sunday of march
        cet=time.localtime(now+3600) # CET:  UTC+1H
    elif now < HHOctober :           # we are before last sunday of october
        cet=time.localtime(now+7200) # CEST: UTC+2H
        print("we are before last sunday of october")
    else:                            # we are after last sunday of october
        cet=time.localtime(now+3600) # CET:  UTC+1H
        print("we are after last sunday of october")
    return(cet)

"""
   set_time() function. Called by main()
   Parameters: None
   Return: None
"""
def set_time():
    # ntp_host = ["0.europe.pool.ntp.org", "1.europe.pool.ntp.org", "2.europe.pool.ntp.org", "3.europe.pool.ntp.org"] # Not used
    
    if not wifi_is_connected:
        print("Wifi is not connected, connecting")
        wifi_connect()
    print("UTC time before synchronization：%s" %str(time.localtime()))
    
    # if needed, we'll cycle over ntp-servers here using:
    # ntptime.host = "1.europe.pool.ntp.org"
    
    try:
        ntptime.settime()
    except OSError as exc:
        if exc.args[0] == 110: # ETIMEDOUT
            print("ETIMEDOUT. Returning False")
            time.sleep(5)
        
    print("UTC time after synchronization：%s" %str(time.localtime()))
    #t = time.localtime(time.time() + UTC_OFFSET) # Apply UTC offset
    t = cet_time()
    print("CET time after synchronization : %s" %str(t))
    
    # Set local clock to adjusted time
    machine.RTC().datetime((t[tm_year], t[tm_mon], t[tm_mday], t[tm_wday] + 1, t[tm_hour], t[tm_min], t[tm_sec], 0))
    print("Local time after synchronization：%s" %str(time.localtime()))    
    time_is_set = True
    #WLAN.disconnect()
    #print("Disconnected WiFi")

"""
    schedule() function
    Parameters: t
    Return: none
"""
import door_operations # script that you want to execute on a certain moment

# Todo: create crontab like structure with: minute / hour / day of month / month / day of week / command
# Todo: check what happens when scheduled tasks overlap (uasyncio)

def schedule(t):
    if t[tm_hour] == 17 and t[tm_min] == 45 and t[tm_sec] == 00: # Define seconds, or it will run every second...
        print("Executing doorOperations")
        door_operations.closeDoor()
    
    if t[tm_hour] == 07 and t[tm_min] == 45 and t[tm_sec] == 00: # Define seconds, or it will run every second...
        print("Executing doorOperations")
        door_operations.openDoor()
    
    # Sync clock every day
    if t[tm_hour] == 08 and t[tm_min] == 15 and t[tm_sec] == 0:
        time_is_set = False
        print("Synchronizing time")
        set_time()

"""
   main() function.
   Parameters: None
   Return: None
"""

def main():
    if not time_is_set:
        set_time()
        
    t = time.localtime()
    o_sec = time.localtime()[5]

    while True:
        t = time.localtime()
        if  o_sec != t[5]:
            o_sec = t[5]
            led.on()
            schedule(t)
            # print(t)
            led.off()



if __name__ == '__main__':
    main()