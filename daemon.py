import io
import logging
import os
from select import select
import socket
import sys
from time import sleep
from PIL import Image, ImageChops
import imagehash
import json
import json
import selectors
import base64


class Daemon: # connects to the network

    def __init__(self, path, port, anchorPort):
        self.path=path
        self.node_id= int(str(abs(hash(path)))[0:4]) # estamos a supor que sendo poucos nodes nao vai haver colisoes
        self.images= {} # (image : hash)
        self.maxSimilarity= 10 # TODO escolher o valor ideal

        # Getting the size of the path
        print("Path ", self.path)
        size = 0
        for path, dirs, files in os.walk(self.path):
            for f in files:
                fp = os.path.join(path, f)
                size += os.path.getsize(fp)
        self.dirSize = size
        print("Size ", self.dirSize)

        self.checkRepeatedImages() # Apaga as imagens repetidas
        self.sel= selectors.DefaultSelector()

        # Daemon info
        self._host = 'localhost'
        self._port = port
        self._address = (self._host, self._port)
        print(f"Deamon {self.node_id} intialized in:{self._address}")
        logging.debug(f"{self.node_id}: Deamon intialized in: {self._address}")

        # Sockets
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        # Anchor Info
        self._anchorPort = anchorPort
        if self._anchorPort == -1:
            self._anchorPort = self._port
            print("Anchor node is:", self._address)
            logging.debug(f"{self.node_id}: Anchor node is: {self._address}")

        # Info about nodes -> Identification
        #  { (ADDRESS) : (ID) }
        self.alive_peers = {}
        # Info about nodes -> Every peer communication needs a socket
        # { (ADDRESS) : (SOCKET) }
        self.socket_peers= {}
        # Numer of connected Peers
        self.connected_peers = 1
        # Number of handshakes
        self.handshake_counter = 0

        # Info about node images
        self.folders_size = {}

        if self._anchorPort == self._port:
            self.folders_size[self._address] = self.dirSize

        # Falut Tolerance: Info about backup nodes
        self.backup_node = None

        # O Daemon tem de ter 2 sockets suas: uma que o permite funcionar como peer e conectar-se a outros peers e uma que o permite funcionar como servidor e receber pedidos
        self.listenSocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listenSocket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listenSocket.bind(self._address)
        self.listenSocket.listen()
        print(f"Listening on port {self._port}!")

        # Peer as a server will be accepting connections
        self.sel.register(self.listenSocket, selectors.EVENT_READ, self.accept)

        if self._anchorPort != self._port:
            self.socket.connect(('localhost', self._anchorPort))
            logging.debug(f"{self.node_id}: Connected to anchor node on port {self._anchorPort}")
            print(f"Connected to anchor node on port {self._anchorPort}")
            self.socket_peers[('localhost', self._anchorPort)] = self.socket 
            self.sel.register(self.socket, selectors.EVENT_READ, self.receive)  # eventos na socket que acabei de aceitar serão tratados pelo read
            msg = {'method': 'HELLOTHERE', 'args': {'new_id': self.node_id, 'from': self._address, 'images' : self.dirSize}}
            size = len(str(msg)).to_bytes(4, byteorder="big")
            msg_in_bytes = size + json.dumps(msg).encode('utf-8')
            self.socket.send(msg_in_bytes)
            #self.socket.setblocking(False))

        # Structure to handle client requests - GetImagesList
        self.ClientImageList = {} # { name : hash }
        self.clientSocket = None
        self.visited_peers = 0

    def connect(self, connection, addr, id):
        """ Connect to socket. """
        connection.connect(addr)
        logging.debug(f"{self.node_id}: Connected to {id} on address {addr}")
        print(f"Connected to {id} on address {addr}")
        self.socket_peers[addr] = connection
        self.alive_peers[addr] = id
        self.connected_peers += 1
        self.sel.register(connection, selectors.EVENT_READ, self.receive)

    def accept(self, sock, mask):
        """ Accept connections from the outside. """    
        (conn, addr) = sock.accept()  # accept connections from the outside
        print(f"Connection accepted: {addr[0]}:{addr[1]}")
        logging.debug(f"{self.node_id}: Connection accepted: {conn}, {addr}")
        # conn.setblocking(False) # estamos prontos para aceitar um novo pedido (dequed)
        self.sel.register(conn, selectors.EVENT_READ, self.receive)  # eventos na socket que acabei de aceitar serão tratados pelo read

    def processDeadPeer(self, connection): 
        """ Follow up processing aftr a peer death. """

        # Eliminating the peer from the list of connected peers 
        for key, value in self.socket_peers.items():
            if value == connection and key != self._address:
                dead_node = key

        print(f"DEAD NODE: {dead_node} died!")
        logging.debug(f"{self.node_id}: DEAD NODE: {dead_node} died!")

        del self.socket_peers[dead_node]
        del self.alive_peers[dead_node]
        self.connected_peers -= 1
        print(self.socket_peers)
        print(self.alive_peers)

        # Elect the anchor node again if necessary
        # Only one of the nodes that detects the dead anchor peer needs to do this
        if dead_node[1] == self._anchorPort:
            print(f"WE NEED A NEW ANCHOR NODE!")
            logging.debug(f"{self.node_id}: WE NEED A NEW ANCHOR NODE!")
            new_anchor_id, new_anchor_addr = self.bully_node(); 
            origin_addr = self._address
            self._anchorPort = new_anchor_addr[1]
            print(f"Anchor node is now: {('localhost', self._anchorPort)}!")
            logging.debug(f"{self.node_id} Anchor node is now: {('localhost', self._anchorPort)}!")
            for addr, conn in self.socket_peers.items(): 
                if addr[1] == self._anchorPort: # 
                    if self._anchorPort == self._address:
                        self.folders_size[self._address] = self.dirSize 
                    msg = {'method':'NEWANCHOR', 'args': {'new_anchor_id' : new_anchor_id, 'new_anchor_addr' : new_anchor_addr, 'from' : origin_addr}}
                    self.send_to(conn, msg)
                else: 
                    msg = {'method':'ELECTIONRESULT', 'args': {'new_anchor_id' : new_anchor_id, 'new_anchor_addr' : new_anchor_addr, 'from' : origin_addr}}
                    self.send_to(conn, msg)

    def receive(self, connection, mask):
        """ Daemon receives messages. """

        conn = connection.recv(4)
        if not conn:
            self.processDeadPeer(connection)
            self.sel.unregister(connection)
            connection.close()
            return 
        else: 
            msg_size = int.from_bytes(conn, byteorder='big')
            jmsg = b''
            while len(jmsg) < msg_size:
                jmsg += connection.recv(msg_size - len(jmsg))
            #jmsg += connection.recv(4)
            msg = json.loads(jmsg.decode('utf-8'))
            print("Received message", msg)

        # métodos exclusivos do main node
        if self._port == self._anchorPort:
            if msg['method'] == 'HELLOTHERE': # receives a 'hellothere' from the new node
                new_id = msg['args']['new_id']
                new_addr = (msg['args']['from'][0], msg['args']['from'][1])
                self.alive_peers[new_addr] = new_id
                self.socket_peers[new_addr] = connection
                self.folders_size[new_addr] = msg['args']['images']
                self.connected_peers += 1
                # Answering the new node with the number of connected peers 
                msg = {'method' : 'GENERALKENOBI', 'args' : { 'origin_id' : self.node_id, 'from' : self._address, 'nodes' : self.connected_peers }}
                self.send_to(connection, msg)
                # Broadcasting to all peers
                print(f"BROADCAST: {new_id} is now in the network!")
                logging.debug(f"{self.node_id}: BROADCAST: {new_id} is now in the network!")
                msgBROADCAST = {'method':'BROADCAST', 'args': { 'new_id': new_id, 'new_addr' : new_addr , 'from' : ('localhost', self._port)}}
                for peer in self.socket_peers:
                    if peer != new_addr: # não vai informar o node que está a entrar na rede
                        self.send_to(self.socket_peers[peer], msgBROADCAST)
            
                # Attributed backup node to new node in the network
                # Attributing a backup node to the anchor node
                if self.backup_node == None: 
                    backupNodeAddr = self.loadBalancer(self._address)
                    print(f"Backup node for node {self._address} is {backupNodeAddr}!")
                    logging.debug(f"Backup node for node {self._address} is {backupNodeAddr}!")
                    backupConnection = self.socket_peers[backupNodeAddr]
                    self.backup_node = backupNodeAddr
                    self.anchorCreateBackup(backupConnection)

                if msg['method'] == 'ASKFORBACKUP':
                    addr = (msg['args']['from'][0], msg['args']['from'][1])
                    # Attributed backup node to new node in the network
                    backupNodeAddr = self.loadBalancer(addr)
                    print(f"Backup node for node {addr} is {backupNodeAddr}!")
                    logging.debug(f"Backup node for node {addr} is {backupNodeAddr}!")
                    # Sending message to new node so that it can send its images to backup
                    backupConnection = self.socket_peers[addr]
                    msgToBackup = {'method':'BACKUP', 'args': {'backup_addr' : backupNodeAddr, 'origin_id' : self.node_id , 'from' : self._address }}
                    self.send_to(backupConnection, msgToBackup)

        if msg['method'] == 'GENERALKENOBI': 
            anchor_id = msg['args']['origin_id']
            anchor_addr = (msg['args']['from'][0], msg['args']['from'][1])
            self.alive_peers[anchor_addr] = anchor_id

        if msg['method'] == 'BROADCAST':
            new_id = msg['args']['new_id']
            new_addr = (msg['args']['new_addr'][0], msg['args']['new_addr'][1])
            newsocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            newsocket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.connect(newsocket, new_addr, new_id)
            print(f"HANDSHAKE {new_id}!")
            logging.debug(f"{self.node_id}: HANDSHAKE {new_id}!")
            msg = {'method':'HANDSHAKE', 'args': { 'node_id' : self.node_id , 'from': ('localhost', self._port)}}
            self.send_to(newsocket, msg)

        if msg['method'] == 'HANDSHAKE':
            connected_id = msg['args']['node_id']
            new_addr = (msg['args']['from'][0], msg['args']['from'][1])
            self.socket_peers[new_addr] = connection
            self.alive_peers[new_addr] = connected_id
            self.handshake_counter += 1
            print(f"I'm now connected to {connected_id}!")
            logging.debug(f"{self.node_id}: I'm now connected to {connected_id}!")
            if (self.connected_peers == self.handshake_counter): 
                msg= {'method':'ASKFORBACKUP', 'args': {'node_id' : self.node_id , 'from': ('localhost', self._port)}}
                self.send_to(self.socket_peers[('localhost', self._anchorPort)], msg)

        if msg['method'] == 'ELECTIONRESULT': 
            self._anchorPort = msg['args']['new_anchor_addr'][1]
            mysize=self.dirSize
            print(f"NEW ANCHOR PORT IS ON {self._anchorPort}")
            msg= {'method':'MYSIZE', 'args': {'node_id' : self.node_id , 'from': ('localhost', self._port), 'size' : mysize}}
            self.send_to(self.socket_peers[('localhost', self._anchorPort)], msg)

        if msg['method'] == 'NEWANCHOR':
            print("I AM THE NEW ANCHOR!")
            self._anchorPort = self._port

        if msg['method'] == 'MYSIZE': 
            addr = (msg['args']['from'][0], msg['args']['from'][1])
            updated_size= msg['args']['size']
            self.folders_size[self._address] = self.dirSize
            self.folders_size[addr] = updated_size

        # Received by daemon -> going to send all of his images to client daemon
        if msg['method'] == 'LISTIMAGE':
            images = self.localImageList()
            conn = self.socket_peers[(msg['args']['from'][0], msg['args']['from'][1])]
            msg= {'method':'LISTREADY', 'args': { 'node_id' : self.node_id , 'from' : self._address, 'list' : images }}
            print(msg)
            #print("listimage msg: ", msg)
            #print(connection)
            print("GETIMAGESLIST: SENDING MY PICS BACK TO PEER TO RETURN TO CLIENT")
            self.send_to(conn, msg)

        # Received by client daemon -> going to receive messages from other peers and send to client
        if msg['method'] == 'LISTREADY':
            print("LIST READY", self.node_id)
            self.visited_peers += 1 
            new_images = msg['args']['list'] 
            originAddr = (msg['args']['from'][0], msg['args']['from'][1])
            for name, hash in new_images.items(): 
                self.ClientImageList[name] = hash
            print("CURRENT PHOTOS")
            if self.visited_peers == len(self.socket_peers) and self.clientSocket != None: 
                print("GOING TO SEND TO CLIENT", {self.clientSocket})
                msg= {'method':'SENDIMAGELIST', 'args': {'id' : self.node_id, 'from' : self._address, 'list' : self.ClientImageList}}
                self.send_to(self.clientSocket, msg)
                self.clientSocket = None
                self.visited_peers = 0

        # Received by client daemon -> going to get all the images in the network
        if msg['method'] == 'GETIMAGELIST':
            self.clientSocket = connection
            images = self.localImageList() # adicionar as suas fotos ao dicionario
            self.ClientImageList = images
            print("GETIMAGESLIST: REQUESTING ALL THE IMAGES IN THE NETWORK")
            print("peers", self.socket_peers)
            for addr, conn in self.socket_peers.items():
                msg = {'method':'LISTIMAGE', 'args': { 'node_id' : self.node_id , 'from' : self._address }}
                self.send_to(conn, msg)
 
        # Daemon finds the image in another node and sends it to the client daemon
        if msg['method'] == 'IMAGE':
            image_size = msg['args']['size']
            image_name = msg['args']['image_name']

            # Building the image so that it can be sent to the client 
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

            msg= {'method':'SENDIMAGE', 'args': {'id' : self.node_id, 'from' : self._address, 'size' : image_size, 'image_name' : image_name}}
            print(f"SENDING TO CLIENT: image {image_name} ...")
            self.send_to(self.clientSocket, msg)
            self.clientSocket.send(image)
            self.clientSocket = None

        # Client daemon tries to find the image in the other nodes
        if msg['method'] == 'FIND':
            hash = msg['args']['hash']
            foundImage = None
            foundImage, image_name = self.checkIfHasImage(hash)
            #msg= {'method':'IMAGE', 'args': { 'node_id' : self.node_id , 'from' : self._address, 'image' : image}}
            #self.send_to(connection, msg)
            #foundImage= self.receive(conn)
            if foundImage != None:
                image_size= len(foundImage)
                print(f"Found Image: {image_name} with the hash {hash} in node {self.node_id}")
                #image, image_name= self.checkIfHasImage(hash) # image ta em bytes
                msg= {'method':'IMAGE', 'args': {'id' : self.node_id, 'from' : self._address, 'size' : image_size, 'image_name' : image_name}}
                self.send_to(connection, msg)
                connection.send(foundImage)

        # Client asks for a specific image 
        if msg['method'] == 'GETIMAGE':
            self.clientSocket = connection
            hash= msg['args']['photo']
            foundImage = None
            #Procura nas suas imagens
            foundImage, image_name= self.checkIfHasImage(hash)

            # envia para os outros nodes caso n encontre para eles procurarem
            if foundImage == None:
                msg= {'method':'FIND', 'args': { 'node_id' : self.node_id , 'from' : self._address, 'hash' : hash}}
                for addr, conn in self.socket_peers.items():
                    self.send_to(conn, msg)
            else: 
                print(f"Found Image: {image_name} with the hash {hash} in node {self.node_id}")
                image_size= len(foundImage)
                msg= {'method':'SENDIMAGE', 'args': {'id' : self.node_id, 'from' : self._address, 'size' : image_size, 'image_name' : image_name}}
                print(f"SENDING TO CLIENT: image {image_name} ...")
                self.send_to(connection, msg)
                connection.send(foundImage)
                self.clientSocket = None

        if msg['method'] == 'BACKUP': 
            backup_addr = (msg['args']['backup_addr'][0], msg['args']['backup_addr'][1])
            self.backup_node = backup_addr
            print(f"BACKUP: My backup node is {backup_addr}!")
            logging.debug(f"{self.node_id}: BACKUP: My backup node is {backup_addr}!") 
            while (backup_addr not in self.socket_peers): 
                conn = self.socket_peers[backup_addr]
            self.createBackup(conn, backup_addr)

        # Recebida pelo no que vai fazer o backup de outro
        if msg['method'] == 'SENDIMAGEBACKUP': 
            number_of_images = msg['args']['amount']
            print("RECEIVING:",number_of_images, "images!")
        
        # Recebida pelo nó que recebeu o backup 
        if msg['method'] == 'BACKUPFINISHED':
            self.checkRepeatedImages() # verifica imagens repetidas
            size = 0
            for path, dirs, files in os.walk(self.path):
                for f in files:
                    fp = os.path.join(path, f)
                    size += os.path.getsize(fp)
            self.dirSize = size            
            msg = {'method' : 'UPDATESIZE', 'args' : { 'new_size' : self.dirSize, 'from' : self._address } }
            print(f"NEW SIZE CALCULATED")
            logging.debug(f"{self.node_id} NEW SIZE CALCULATED")
            self.send_to(connection, msg)

        if msg['method'] == 'UPDATESIZE': 
            addr = (msg['args']['from'][0], msg['args']['from'][1])
            new_size = msg['args']['new_size']
            self.folders_size[addr] = new_size
            print(f"NEW SIZE OF {addr} is {new_size}")
            logging.debug(f"{self.node_id} NEW SIZE CALCULATED")

        if msg['method'] == 'SENDIMAGE':
            image= None
            image_size = msg['args']['size']
            image_name= msg['args']['image_name']
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
            formato= image.format
            image.save(self.path+image_name, formato)          

    def anchorCreateBackup(self, connection): 
        """ Choosing a backup node for the anchor node. """
        for path in self.images:
            hash = self.images[path]
            with open(path, 'rb') as image_file:
                image = base64.b64encode(image_file.read()).decode('utf-8')
                image= json.dumps(image).encode('utf-8')
            image_size = len(image)
            print(f"Sending image {hash} to backup node!")
            print("image_size", image_size)
            logging.debug(f"{self.node_id}: Sending image {hash} to backup node!")
            image_name = path.replace(self.path, "")
            msg= {'method':'SENDIMAGE', 'args': {'id' : self.node_id, 'from' : self._address, 'size' : image_size, 'image_name' : image_name}}
            self.send_to(connection, msg)
            connection.send(image)  
        msgBackupFinish = {'method' : 'BACKUPFINISHED', 'args' : {'from' : self._address }}
        self.send_to(connection, msgBackupFinish)      

    def createBackup(self, connection, backup_addr):
        """ Sending images to backup node """
        print("STARTING BACKUP PROCESS")
        number_of_images= len(self.images)
        msg= {'method':'SENDIMAGEBACKUP', 'args': {'id' : self.node_id, 'from' : self._address, 'amount' : number_of_images}}
        self.send_to(connection, msg)
        for path in self.images:
            hash = self.images[path]
            with open(path, 'rb') as image_file:
                image = base64.b64encode(image_file.read()).decode('utf-8')
                image= json.dumps(image).encode('utf-8')
            image_size = len(image)
            print(f"Sending image {hash} to backup node!")
            print("image_size", image_size)
            logging.debug(f"{self.node_id}: Sending image {hash} to backup node!")
            image_name = path.replace(self.path, "")
            msg= {'method':'SENDIMAGE', 'args': {'id' : self.node_id, 'from' : self._address, 'size' : image_size, 'image_name' : image_name}}
            self.send_to(connection, msg)
            connection.send(image)
        msgBackupFinish = {'method' : 'BACKUPFINISHED', 'args' : {'from' : self._address }}
        self.send_to(connection, msgBackupFinish)

    def send_to(self, connection : socket, msg):
        """ Sending messages through socket. """
        size = len(str(msg)).to_bytes(4, byteorder="big")
        msg_in_bytes =size + json.dumps(msg).encode('utf-8')
        connection.send(msg_in_bytes)

    def loadBalancer(self, nodeToBackUp):
        """ Loadbalancer to choose a backup node. """
        """ Also stabilizes the amount of pictures in each node. """
        """ Chooses based on the size of the folder. """
        print(f"LOADBALANCER: CHOOSING BACKUP FOR {nodeToBackUp}")
        logging.debug(f"{self.node_id}: LOADBALANCER: CHOOSING BACKUP FOR {nodeToBackUp}")
        temp_dict = dict(self.folders_size)
        del temp_dict[nodeToBackUp]
        minSize = 0
        counter = 0
        for addr, size in temp_dict.items(): 
            if size < list(temp_dict.values())[minSize] and  addr != nodeToBackUp:
                minSize = counter
            counter += 1
        bestBackupAddr = list(temp_dict.keys())[minSize]
        
        # Updating value in folder size 
        self.folders_size[bestBackupAddr] += self.dirSize 
     
        # TODO update policy
        return bestBackupAddr
        
    def bully_node(self):
        """" Choosing a coordinator node. """
        print("SELECTING NEW ANCHOR NODE")
        temp_dict = dict(self.alive_peers)
        temp_dict[self._address] = self.node_id 
        maximo = max(temp_dict, key=temp_dict.get)
        return  temp_dict[maximo], maximo


    def checkRepeatedImages(self):
        """ Loops through its directory checking for repeated images using Image Hash. Deletes one of them each time there's similarities"""
        first = True
        count=0

        i = 10
        progress= "[..........]"

        if len(self.path) <= 1: # probaly desnecessario mas so para o caso
            return
            
        for filename in os.listdir(self.path):  
            similar= False
            image = os.path.join(self.path, filename)
            count+=1
            #print(count)



            #fancy progress bar c:
            if int(count*100/len(self.path)) / i > 1:
                progress= progress.replace(".", "#", 1)
                print(progress, "{}%".format(i))
                i += 10
            if count==len(self.path):
                print("[##########]", "100%")



            
            if os.path.isfile(image): # checking if it is a file
                hash = imagehash.average_hash(Image.open(image))

                if first:
                    self.images[image] = hash    
                    first = False
                else:
                    for old_image in self.images:
                        if hash - self.images[old_image] < self.maxSimilarity:
                            similar= True
                            self.pickTheBestImage(image , old_image, hash)
                            break
                    if not similar:
                        self.images[image] = hash    
                        similar= False

    def pickTheBestImage(self, new_image, old_image, new_image_hash): # escolhe a que tiver cor ou (se ambas tiverem ou nao) a que tem o maior tamanho
        """ Pick the best image. """

        imagem_nova = Image.open(new_image)
        width, height = imagem_nova.size
        new_size= width * height


        imagem_antiga = Image.open(old_image)
        width, height = imagem_antiga.size
        old_size= width * height
        if ((self.isGreyScale(imagem_nova) and  self.isGreyScale(imagem_antiga)) or not ( self.isGreyScale(imagem_nova) or self.isGreyScale(imagem_antiga))): # ambas com cor ou preto e branco
            print("PICKING THE BEST IMAGE: SAME COLOUR SCHEME")
            print(old_image,"\n", new_image)
            if new_size > old_size:
                del self.images[old_image]
                self.size = self.dirSize - 1
                os.remove(old_image) # deletes file
                self.images[new_image] = new_image_hash
            else:
                os.remove(new_image) # deletes file

        elif (self.isGreyScale(imagem_antiga) and not self.isGreyScale(imagem_nova)): # so a antiga e a preto e branco
            print("PICKING THE BEST IMAGE: DIFFERENT COLOUR SCHEME")
            print(old_image,"\n", new_image)
            del self.images[old_image]
            self.size = self.dirSize - 1
            os.remove(old_image) # deletes file
            self.images[new_image] = new_image_hash
        else: # mantem a antiga
            os.remove(new_image) # deletes file

    def isGreyScale(self, image):
        if image.mode == "RGB":
            rgb = image.split()
            if ImageChops.difference(rgb[0],rgb[1]).getextrema()[1]!=0: 
                return False
            if ImageChops.difference(rgb[0],rgb[2]).getextrema()[1]!=0: 
                return False
        return True

    def localImageList(self):
        localImages = {}
        for filename in os.listdir(self.path):
            image = os.path.join(self.path, filename)
            image_hash=  imagehash.average_hash(Image.open(image))
            localImages[filename] = str(image_hash)
        return localImages

    def checkIfHasImage(self, hash):
        foundImage= None
        filename= None
        #procura nas suas imagens
        for filename in os.listdir(self.path):
            file = os.path.join(self.path, filename)
            image_hash=  imagehash.average_hash(Image.open(file))
            if str(image_hash) == hash:
                foundImage = os.path.join(self.path, filename)
                """foundImage = Image.open(foundImage)"""
                with open(foundImage, 'rb') as image_file:
                    foundImage = base64.b64encode(image_file.read()).decode('utf-8')
                    foundImage= json.dumps(foundImage).encode('utf-8')
                break
        return foundImage, filename

    def run(self):
        try:
            while True:
                events= self.sel.select(timeout=None)
                for key, mask in events:
                    callback = key.data
                    callback(key.fileobj, mask)
        except KeyboardInterrupt:
            self.socket.close()

if __name__ == "__main__":
    if len(sys.argv)<3 or len(sys.argv)>4:
        print("args needed: path port [anchor port]")
        quit()
    path= str(sys.argv[1])
    if not path.endswith('/'):
        path += '/'
    port= int(sys.argv[2])
    anchorPort=-1
    if len(sys.argv)==4:
        anchorPort= int(sys.argv[3])
    dicionario = {} # address: socket
    daemon = Daemon(path, port, anchorPort)
    daemon.run()