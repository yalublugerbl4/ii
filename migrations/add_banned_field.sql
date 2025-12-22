-- Миграция: добавление поля banned в таблицу users
-- Выполнить: psql -d your_database -f migrations/add_banned_field.sql

DO $$
BEGIN
    -- Добавляем banned в users, если его нет
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'users'
        AND column_name = 'banned'
    ) THEN
        ALTER TABLE users ADD COLUMN banned BOOLEAN DEFAULT FALSE NOT NULL;
    END IF;
END $$;

