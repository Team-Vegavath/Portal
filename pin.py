import bcrypt

pin = "<Textornum>".encode()
print(bcrypt.hashpw(pin, bcrypt.gensalt()).decode())
