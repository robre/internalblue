#!/usr/bin/python3

# Jiska Classen, Secure Mobile Networking Lab

from pwn import *
from internalblue.hcicore import HCICore
import internalblue.hci as hci
import numpy as np
from datetime import datetime

"""
Measure the RNG of the CYW20735 Evaluation Board.
Similar to matedealer's thesis, p. 51.

Changes:

* Every 5th byte is now 0x42 to ensure that no other process wrote
  into this memory region in the meantime. Does it job and cheaper
  than checksums.

* When we are done, we send an HCI event containing 'RAND'. We catch
  this with a callback. Way more efficient than polling.

* We overwrite the original `rbg_rand` function with `bx lr` to
  ensure we're the only ones accessing the RNG.
  
* BT only, no need to disable Wi-Fi.

* Launch_RAM is also broken on this one :D

"""

ASM_LOCATION_RNG = 0x217000  # load our snippet into Patchram (we need to disable all patches for this!)
MEM_RNG = ASM_LOCATION_RNG + 0xf0  # store results here
MEM_ROUNDS = 0x500  # run this often (x5 bytes)
FUN_RNG = 0xA562E  # original RNG function that we overwrite with bx lr

ASM_SNIPPET_RNG = """

    // use r0-r7 locally
    push {r0-r7, lr} 
    
    // send a command complete event as we overwrote the launch_RAM handler to prevent HCI timeout event wait
    mov  r0, #0xFC4E // launch RAM command
    mov  r1, 0       // event success
    bl   0x24E66     // bthci_event_SendCommandCompleteEventWithStatus
    
    // enter RNG dumping mode
    ldr  r0, =0x%x      // run this many rounds
    ldr  r1, =0x%x      // dst: store RNG data here
    bl   dump_rng
    
    // done, let's notify
    bl   notify_hci
    
    // back to lr
    pop  {r0-r7, pc}
    
    
    //// the main RNG dumping routine
    dump_rng:
    
    // wait until RNG is ready, which is indicated by status 0x200fffff
    wait_ready:
        ldr  r2,=0x352604
        ldr  r2, [r2]
        ldr  r3, =0x200fffff
        cmp  r2, r3
        bne  wait_ready  
    
    // request new entropy: rbg_control_adr=1
    mov  r3, 1
    ldr  r2, =0x352600
    str  r3, [r2]
    
    // dst is in r1, dump RNG value here
    ldr  r2, =0x352608
    ldr  r3, [r2]
    str  r3, [r1]
    add  r1, 4 
    
    // add a test byte to ensure that no other process wrote here
    mov  r3, 0x42
    str  r3, [r1]
    add  r1, 1
    
    // loop for rounds in r0
    subs r0, 1
    bne  dump_rng
    bx   lr
    
    
    
    //// issue an HCI event once we're done
    notify_hci:
        
    push  {r0-r4, lr}

    // allocate vendor specific hci event
    mov  r2, 243
    mov  r1, 0xff
    mov  r0, 245
    bl   0x24E92    // bthci_event_AllocateEventAndFillHeader
    mov  r4, r0     // save pointer to the buffer in r4

    // append buffer with "RAND"
    add  r0, 10  // buffer starts at 10 with data
    ldr  r1, =0x444e4152 // RAND
    str  r1, [r0]
    add  r0, 4      // advance buffer by 4

    // send hci event
    mov  r0, r4     // back to buffer at offset 0
    bl   0x24C36    // bthci_event_AttemptToEnqueueEventToTransport
    
    
    pop   {r0-r4, pc}
    
    
""" % (MEM_ROUNDS, MEM_RNG)


internalblue = HCICore()
internalblue.interface = 'hci0'  # internalblue.device_list()[0][1]  # just use the first device

# setup sockets
if not internalblue.connect():
    log.critical("No connection to target device.")
    exit(-1)

progress_log = log.info("Installing assembly patches...")


# Install the RNG code in RAM
code = asm(ASM_SNIPPET_RNG, vma=ASM_LOCATION_RNG)
if not internalblue.writeMem(address=ASM_LOCATION_RNG, data=code, progress_log=progress_log):
    progress_log.critical("error!")
    exit(-1)

# Disable original RNG
patch = asm("bx lr; bx lr", vma=FUN_RNG)  # 2 times bx lr is 4 bytes and we can only patch 4 bytes
if not internalblue.patchRom(FUN_RNG, patch):
    log.critical("Could not disable original RNG!")
    exit(-1)

# CYW20735 Launch_RAM fix: overwrite an unused HCI handler
# The Launch_RAM handler is broken so we can just overwrite it to call the function we need.
# The handler table entry for it is at 0x1425BC, and it points to launch_RAM+1.
# Located by looking for bthci_cmd_vs_HandleLaunch_RAM+1 in the dump.
if not internalblue.patchRom(0x1425BC, p32(ASM_LOCATION_RNG+1)):  # function table entries are sub+1
    log.critical("Could not implement our launch RAM fix!")
    exit(-1)



log.info("Installed all RNG hooks.")





"""
We cannot call HCI Read_RAM from this callback as it requires another callback (something goes wrong here),
so we cannot solve this recursively but need some global status variable. Still, polling this is way faster
than polling a status register in the Bluetooth firmware itself.
"""
# global status
internalblue.rnd_done = False
def rngStatusCallback(record):
    hcipkt = record[0]  # get HCI Event packet

    if not issubclass(hcipkt.__class__, hci.HCI_Event):
        return

    if hcipkt.data[0:4] == bytes("RAND", "utf-8"):
        log.debug("Random data done!")
        internalblue.rnd_done = True

# add RNG callback
internalblue.registerHciCallback(rngStatusCallback)


#cli.commandLoop(internalblue)


# read for multiple rounds to get more experiment data
rounds = 1000
i = 0
data = bytearray()
while rounds > i:
    log.info("RNG round %i..." % i)

    # launch assembly snippet
    internalblue.launchRam(ASM_LOCATION_RNG)

    # wait until we set the global variable that everything is done
    while not internalblue.rnd_done:
        continue
    internalblue.rnd_done = False

    # and now read and save the random
    random = internalblue.readMem(MEM_RNG, MEM_ROUNDS*5)

    # do an immediate check to tell where the corruption happened
    check = random[4::5]
    pos = 0
    failed = False
    for c in check:
        pos = pos + 1
        if c != 0x42:
            log.warn("    Data was corrupted at 0x%x, repeating round." % (MEM_RNG+(pos*5)))
            failed = True
            break

    if failed:
        continue

    # no errors, save data
    data.extend(random)
    i = i + 1

log.info("Finished acquiring random data!")

# uhm and for deleting every 5th let's take numpy (oh why??)
data = np.delete(data, np.arange(4, data.__len__(), 5))


f = open("cyw20735-randomdata-%irounds-0x500-%s.bin" % (rounds, datetime.now()), "wb")
f.write(data)
f.close()


#log.info("--------------------")
#log.info("Entering InternalBlue CLI to interpret RNG.")

## enter CLI
#cli.commandLoop(internalblue)

