-- Миграция: добавление колонки email в таблицу users
-- Выполнить: psql -d your_database -f migrations/add_email_to_users.sql

-- Проверяем, существует ли колонка, и добавляем её если нет
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 
        FROM information_schema.columns 
        WHERE table_name = 'users' 
        AND column_name = 'email'
    ) THEN
        ALTER TABLE users ADD COLUMN email VARCHAR(255) NULL;
    END IF;
END $$;

