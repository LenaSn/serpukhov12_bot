import ssl, socket

ctx = ssl.create_default_context()
s = ctx.wrap_socket(socket.socket(), server_hostname="api.telegram.org")
s.connect(("api.telegram.org", 443))
print("OK")