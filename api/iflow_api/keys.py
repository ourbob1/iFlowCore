"""Print a new Ed25519 signing key pair for IFLOW_API_JWT_PRIVATE_KEY (store it in Secrets Manager).

    python -m iflow_api.keys
"""

from iflow_api.settings import generate_keypair_pem

if __name__ == "__main__":
    private, public = generate_keypair_pem()
    print("# IFLOW_API_JWT_PRIVATE_KEY (secret: put it in your secret store, never in git)")
    print(private)
    print("# public key (for IFLOW_API_JWT_PREVIOUS_KEYS after the next rotation)")
    print(public)
