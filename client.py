import base64
import io
import json
import socket
import selectors
import logging
import sys
import os
import fcntl
from PIL import Image


class Client:
    """ Class Client. """

    orig_fl = fcntl.fcntl(sys.stdin, fcntl.F_GETFL)
    fcntl.fcntl(sys.stdin, fcntl.F_SETFL, orig_fl | os.O_NONBLOCK)

    logging.basicConfig(filename=f"{sys.argv[0]}.log", level=logging.DEBUG)

    def __init__(self, daemon_addr, daemon_port):
        self._host = daemon_addr
        self._port = daemon_port
        self._address= (daemon_addr, daemon_port)

        self.request = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sel = selectors.DefaultSelector()
        self.sel.register(self.request, selectors.EVENT_READ, self.read)
        self.sel.register(sys.stdin, selectors.EVENT_READ, self.get_keyboard_data)

    def connect(self):
        self.request.connect(self._address)
        logging.debug(f"Connection to server on port {self._port}")
        print(f"Connected to server on port {self._port}")
        self.request.setblocking(False)

    def loop(self):
        """Loop indefinetely."""
        try:
            while True:
                print("\nMenu options:\nGetImagesList\nGetImage (hash)\nExit")
                for key, mask in self.sel.select():  # list of ready objects
                    callback = key.data
                    callback(key.fileobj)
        except KeyboardInterrupt:  # ctrl+c to close server
            self.request.close()

    def get_keyboard_data(self, stdin):
        input  = stdin.read() 
        self.write(input)

    def write(self, input):
        """ Sending requests to daemon. """

        stdInput = input.strip().split(" ")
        if stdInput[0].lower() == "getimageslist":  # juntar-se ao canal de mensagens
            msg= {'method':'GETIMAGELIST', 'args': {'from' : self._address}}
            self.send(self.request, msg)
        elif stdInput[0].lower() == "getimage":
            if len(stdInput) == 2:
                hash= stdInput[1]
                msg= {'method':'GETIMAGE', 'args': {'from' : self._address, 'photo' : hash}}
                self.send(self.request, msg)
            else:
                print("ERROR: missing arg: hash\n")
        elif stdInput[0].lower() == "exit":  # terminar programa cliente
            self.request.close()
            quit()
        else:
            print("ERROR: command not found\n")
        sys.stdout.flush()

    def read(self, connection):
        conn = connection.recv(4)
        msg_size = int.from_bytes(conn, byteorder='big')
        jmsg = b''
        while len(jmsg) < msg_size:
            jmsg += connection.recv(msg_size - len(jmsg))
        #jmsg += connection.recv(4)
        msg = json.loads(jmsg.decode('utf-8'))

        # processamento da resposta do daemon -> later 
        if msg['method'] == 'SENDIMAGELIST':
            mensagem=  msg['args']['list']
            print("--------------------------------------------------IMAGES IN THE NETWORK--------------------------------------------------")
            for name, hash in mensagem.items():
                print(f"IMAGE {name} of hash {hash}")
            print("-------------------------------------------------------------------------------------------------------------------------")

        if msg['method'] == 'SENDIMAGE':
            image= None
            image_size = msg['args']['size']
            to_read= image_size
            while to_read > 0:
                image_recv = connection.recv(to_read)
                to_read -= len(image_recv)
                if image is None:
                    image= image_recv
                else:
                    image+=image_recv
            image= json.loads(image)
            decoded_image_data = base64.b64decode(image)
            base64_img_bytes = decoded_image_data
            image = Image.open(io.BytesIO(base64_img_bytes))
            image.show()

        #self.kill_client()

    def send( self, connection, msg ):
        """Sends request through a socket connection."""
        size = len(str(msg)).to_bytes(4, byteorder="big")
        msg_in_bytes =size + json.dumps(msg).encode('utf-8')
        connection.send(msg_in_bytes)
        
    def kill_client(self):
        """ Kills client after the request is done. """
        self.request.close()
        quit()

if __name__ == "__main__":
    print(f"Arguments count: {len(sys.argv)}")
    daemon_port = int(sys.argv[1])
    daemon_host = 'localhost'
    client = Client(daemon_host, daemon_port)
    client.connect()
    client.loop()