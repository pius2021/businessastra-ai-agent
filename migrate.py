import asyncio
import aiomysql
from config import DB_HOST, DB_PORT, DB_USER, DB_PASS, DB_NAME

async def migrate():
    pool = await aiomysql.create_pool(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASS, db=DB_NAME, autocommit=True
    )
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME   = 'customers'
                  AND COLUMN_NAME  = 'call_status'
            """)
            row = await cur.fetchone()
            if row[0] == 0:
                await cur.execute("""
                    ALTER TABLE customers
                    ADD COLUMN call_status
                    ENUM('pending','in_progress','completed','failed') DEFAULT 'pending'
                """)
                print("Migration applied: call_status column ADDED successfully")
            else:
                print("No migration needed — call_status column already exists")
    pool.close()
    await pool.wait_closed()

asyncio.run(migrate())
