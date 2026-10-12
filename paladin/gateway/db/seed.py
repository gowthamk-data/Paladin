from passlib.hash import bcrypt
from gateway.db.session import init_db
from gateway.db.models import User

# Demo accounts. `fin1` is the Finance-department user added for the ABAC access
# matrix (a level-2 Finance employee, so it can read Finance + company-wide docs
# but not Engineering/Executive-scoped ones).
SEED_USERS = [
    {"username": "emp1", "password": "emp1_pass", "department": "Engineering", "clearance_level": 1, "role": "employee"},
    {"username": "mgr1", "password": "mgr1_pass", "department": "Engineering", "clearance_level": 2, "role": "manager"},
    {"username": "admin1", "password": "admin1_pass", "department": "Executive", "clearance_level": 3, "role": "admin"},
    {"username": "fin1", "password": "fin1_pass", "department": "Finance", "clearance_level": 2, "role": "employee"},
]


def seed_users():
    """Create any missing demo users without touching existing rows.

    Safe to rerun against an already-seeded database: a username that already
    exists is skipped entirely (its password hash and attributes are preserved),
    and only missing accounts are inserted.
    """
    db = init_db()
    try:
        created = 0
        for user_data in SEED_USERS:
            existing = db.query(User).filter(User.username == user_data["username"]).first()
            if existing is not None:
                print(f"[SEED] {user_data['username']} already exists, skipping.")
                continue
            password_hash = bcrypt.hash(user_data["password"])
            user = User(
                username=user_data["username"],
                password_hash=password_hash,
                department=user_data["department"],
                clearance_level=user_data["clearance_level"],
                role=user_data["role"],
            )
            db.add(user)
            created += 1
            print(f"[SEED] Created user: {user_data['username']} (clearance: {user_data['clearance_level']}, role: {user_data['role']})")
        db.commit()
        print(f"[SEED] Seeding complete. {created} created, {len(SEED_USERS) - created} already existed.")
    finally:
        db.close()


if __name__ == "__main__":
    seed_users()
