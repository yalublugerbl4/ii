-- Добавляем поля для реферальной программы
ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by BIGINT REFERENCES users(tgid);

-- Создаем индекс для быстрого поиска
CREATE INDEX IF NOT EXISTS idx_users_referred_by ON users(referred_by);

