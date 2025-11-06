import bcrypt

pin = "Admin123".encode()
print(bcrypt.hashpw(pin, bcrypt.gensalt()).decode())
