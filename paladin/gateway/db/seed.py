from passlib.hash import bcrypt
from gateway.db.session import init_db, get_db
from gateway.db.models import User

SEED_USERS = [
    {"username": "emp1", "password": "emp1_pass", "department": "Engineering", "clearance_level": 1, "role": "employee"},
    {"username": "mgr1", "password": "mgr1_pass", "department": "Engineering", "clearance_level": 2, "role": "manager"},
    {"username": "admin1", "password": "admin1_pass", "department": "Executive", "clearance_level": 3, "role": "admin"},
]


def seed_users():
    db = init_db()
    try:
        existing = db.query(User).count()
        if existing > 0:
            print(f"[SEED] {existing} user(s) already exist, skipping seed.")
            return
        for user_data in SEED_USERS:
            password_hash = bcrypt.hash(user_data["password"])
            user = User(
                username=user_data["username"],
                password_hash=password_hash,
                department=user_data["department"],
                clearance_level=user_data["clearance_level"],
                role=user_data["role"],
            )
            db.add(user)
            print(f"[SEED] Created user: {user_data['username']} (clearance: {user_data['clearance_level']}, role: {user_data['role']})")
        db.commit()
        print("[SEED] Database seeded successfully.")
    finally:
        db.close()


if __name__ == "__main__":
    seed_users()
