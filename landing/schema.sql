CREATE TABLE IF NOT EXISTS products (
  channel TEXT NOT NULL,
  num INTEGER NOT NULL,
  name TEXT NOT NULL,
  url TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (channel, num)
);
