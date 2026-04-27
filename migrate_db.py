from sqlalchemy import text
from src.db.models import get_engine

def migrate():
    engine = get_engine()
    with engine.connect() as conn:
        try:
            conn.execute(text('ALTER TABLE vehicle_positions ADD COLUMN is_stationary BOOLEAN DEFAULT 0;'))
            conn.commit()
            print("Successfully added is_stationary column.")
        except Exception as e:
            print(f"Error (might already exist): {e}")

if __name__ == "__main__":
    migrate()
