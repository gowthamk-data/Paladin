#!/bin/bash
mkdir -p "$(dirname "$0")/../keys"
openssl genrsa -out "$(dirname "$0")/../keys/private.pem" 2048
openssl rsa -in "$(dirname "$0")/../keys/private.pem" -pubout -out "$(dirname "$0")/../keys/public.pem"
chmod 600 "$(dirname "$0")/../keys/private.pem"
echo "RSA keys generated in keys/"
