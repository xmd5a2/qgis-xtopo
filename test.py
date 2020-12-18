import time
starttime = time.time()
while True:
    print("tick")
    time.sleep( 1 - ((time.time() - starttime) % 1))