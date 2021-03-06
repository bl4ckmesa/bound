#! /usr/bin/env python

import socket,select
import binascii
import re
import sqlite3
import pyjsonrpc
import time
import dns.resolver
import dns.reversename
import sys

# Utility function(s)
def is_ipv4(ip):
	match = re.match("^(\d{0,3})\.(\d{0,3})\.(\d{0,3})\.(\d{0,3})$", ip)
	if not match:
		return False
	quad = []
	for number in match.groups():
		quad.append(int(number))
	if quad[0] < 1:
		return False
	for number in quad:
		if number > 255 or number < 0:
			return False
	return True

# message should be hex encoded
def parse_query(message):
	m = {}
	# Example hex values for www.google.edu nslookup
	# ab5a010000010000000000000377777706676f6f676c65036564750000010001
	# ID [ab5a], Flags [0100], QDCOUNT [0001], ANCOUNT [0000], NSCOUNT [0000], ARCOUNT [0000]
	m['id'], m['flags'], m['qdcount'], m['ancount'], m['nscount'], m['arcount'] = re.findall('....', message[0:24])
	domains = []
	msg_contents = message[24:]
	i = 0
	while i < len(msg_contents):
		byte_width = 2
		# Denotes how long the domain string length will be, in hex. 
		# Convert to int, multiply by byte char width (usually 2)
		domain_length = int(msg_contents[i:i+byte_width], 16) * byte_width
		if domain_length != 0:
			d = msg_contents[i+byte_width:i+domain_length+byte_width]
			dstring = binascii.unhexlify(d)
			domains.append(dstring)
			i += domain_length + byte_width
		else:
			#print "Message Domain:", '.'.join(domains)
			m['domains'] = domains
			# Done going through domains segments, now get final bits
			# QueryType [0001], DataClass [0001]
			# Common QueryTypes:
			# 0001 A (Normal query)
			# 0002 NS
			# 0005 CNAME
			# 0006 SOA
			# 000c PTR
			# 0010 TXT
			# 001c AAAA
			# 0026 A6
			# 00fb IXFR
			# 00fc AXFR
			# 00ff wildcard
			i += 2
			m['querytype'] = msg_contents[i:i+4]
			i += 4
			m['dataclass'] = msg_contents[i:i+4]
			# Running 'dig' also added a bunch more stuff at the end.  Here's an example:
			# 0000291000000000000000
			# Ignoring until I think/know I need it.
			break
	return m

# Returns binary response
def gen_response(request,serverip):
	# Example hex values for www.google.edu response w/ 1.2.5.33 for ip address
	# ab5a818000010001000000000377777706676f6f676c65036564750000010001c00c0001000100000020000401020521
	response=''
	response += request['id']
	if serverip == "0.0.0.0":
		response_flags = '8183' # Not found
	elif "in-addr" in serverip:	
		print "IP is actually the rDNS response"
	else:
		response_flags = '8180' # Normal Reponse
	response += response_flags
	response += request['qdcount']
	response_ancount = '0001'
	response += response_ancount
	response += request['nscount']
	response += request['arcount']

	# Re-encode domains and add to response
	domains = request['domains']
	for domain in domains:
		dlen = str(hex(len(domain))[2:]).zfill(2)
		response += dlen
		d = binascii.hexlify(domain)
		#print "Domain:", d
		response += d
	response += '0000010001' # End of the original query, probably ;)

	response_ispointer = 'c'
	response += response_ispointer
	response_nameoffset = '00c'
	response += response_nameoffset
	response_type = '0001' # 0001 = Type A query (Host address)
	response += response_type
	response_class = '0001' # 0001 = Class IN (Internet address)
	response += response_class
	response_ttl = '00000020' # 20 = 32 decimal seconds.
	response += response_ttl
	address_length = '0004'
	response += address_length
	if is_ipv4(serverip):
		response += binascii.b2a_hex(str.join('',map(lambda x: chr(int(x)), serverip.split('.'))))
	else:
		servername = serverip.split('.')
		for servlet in servername:
			response += binascii.hexlify(str(hex(len(servlet))[2:]).zfill(2))
			response += binascii.hexlify(servlet)
	return response

def get_serverip(url,msg):
	data = "0.0.0.0"
	print "Getting IP for", url
	conn = sqlite3.connect(r"bound.db")
	cur = conn.cursor()
	cur.execute('CREATE TABLE IF NOT EXISTS A ( URL TEXT PRIMARY KEY NOT NULL, IP text NOT NULL )')
	if msg['querytype'] == '0001':
		try:
			cur.execute('SELECT IP FROM A WHERE URL = "'+url+'"')
			data = cur.fetchone()[0]
		except:
			# Try looking it up manually
			try:
				## Code from stackoverflow
				# import dns.resolver 
				# pip install dnspython
				my_resolver = dns.resolver.Resolver()
				# 8.8.8.8 is Google's openDNS server
				my_resolver.nameservers = ['192.168.16.31']
				resolved = my_resolver.query(url, 'A')
				data = str(resolved[0])
				#data = socket.gethostbyname(url)
			except Exception, e:
				print "Error:", e
				data = "0.0.0.0"
	elif msg['querytype'] == '000c':
		# Example reverse DNS: 4.3.2.1-in-addr.arpa: type PTR, class IN
		ipaddr = ".".join(msg['domains'][:4][::-1])
		print "Got an RDNS Query for", ipaddr
		try:
			cur.execute('SELECT URL FROM A WHERE IP = "'+ipaddr+'"')
			data = cur.fetchone()[0]
		except:
			print "IP Addr not found in A"
			# Try looking it up manually
			try:
				addr = dns.reversename.from_address(ipaddr)
				data = str(dns.resolver.query(addr, 'PTR')[0])
			except Exception, e:
				print "rDNS Error:", e
				data = "0.0.0.0"
		#data = "0.0.0.0"
	conn.commit()
	print "Data came out to:", data
	return data

UDP_IP = '0.0.0.0'
UDP_PORT = 53

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM) # UDP
sock.bind((UDP_IP, UDP_PORT))
sock2 = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_UDP)
sock2.bind((UDP_IP, UDP_PORT))

print "Listening on port %s..." % UDP_PORT
sys.stdout.flush()

while True:
	#print "Got it.", time.time()
	r, w, x = select.select([sock, sock2], [], [], 0.01)
	#print "Waiting...", time.time()
	#time.sleep(0.01)
	for i in r:
		#print "Got a packet!"
		packet = i.recvfrom(128)
		message = binascii.b2a_hex(packet[0])
		ip = packet[1][0]
		port = packet[1][1]

		if port is not 0: # All those broadcast messages from Bonjour! ;)
			# Get requested domain
			msg = parse_query(message)
			url = ".".join(msg['domains'])
			print "Request:", ip, port, url
			sys.stdout.flush()
			print "Message:", message

			# Get IP Address for given domain
			serverip = get_serverip(url,msg)

			# Respond with IP Address for requested domain
			print "Answer:", serverip, url
			sys.stdout.flush()
			i.sendto(binascii.a2b_hex(gen_response(msg,serverip)), (ip, port))
