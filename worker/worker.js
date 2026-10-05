/**
 * Voting App - Worker service
 *
 * Blocks on the Redis "vote_queue" list. For every job it pulls off,
 * it validates the user/option exist, inserts a row into the "vote"
 * table in Postgres, and moves on to the next job.
 *
 * Flask writes the "user" and "option" tables (via SQLAlchemy's
 * db.create_all()); this worker only ever reads from those and
 * inserts into "vote".
 */

require("dotenv").config();

const { Pool } = require("pg");
const { createClient } = require("redis");

const REDIS_URL = process.env.REDIS_URL || "redis://localhost:6379/0";
const DATABASE_URL =
  process.env.DATABASE_URL ||
  "postgresql://voting:voting@localhost:5432/voting";
const VOTE_QUEUE_KEY = "vote_queue";

const pool = new Pool({ connectionString: DATABASE_URL });

/**
 * A job is valid if it has both a user_id and an option_id.
 * Pulled out as its own function so it can be unit tested without
 * needing a real Postgres/Redis connection.
 */
function isValidJob(job) {
  return Boolean(job && job.user_id && job.option_id);
}

async function processJob(job) {
  if (!isValidJob(job)) {
    console.error("Skipping malformed job:", job);
    return;
  }

  const { user_id, option_id } = job;

  const client = await pool.connect();
  try {
    await client.query("BEGIN");

    const userCheck = await client.query(
      'SELECT id FROM "user" WHERE id = $1',
      [user_id]
    );
    const optionCheck = await client.query(
      'SELECT id FROM "option" WHERE id = $1',
      [option_id]
    );

    if (userCheck.rowCount === 0 || optionCheck.rowCount === 0) {
      console.error(
        `Skipping job for missing user/option: user_id=${user_id} option_id=${option_id}`
      );
      await client.query("ROLLBACK");
      return;
    }

    await client.query(
      'INSERT INTO "vote" (user_id, option_id, created_at) VALUES ($1, $2, NOW())',
      [user_id, option_id]
    );

    await client.query("COMMIT");
    console.log(`Recorded vote: user_id=${user_id} option_id=${option_id}`);
  } catch (err) {
    await client.query("ROLLBACK");
    console.error("Failed to process job, rolled back:", err.message);
  } finally {
    client.release();
  }
}

async function main() {
  const redisClient = createClient({ url: REDIS_URL });
  redisClient.on("error", (err) => console.error("Redis error:", err));

  await redisClient.connect();
  console.log("Worker connected to Redis and Postgres. Waiting for votes...");

  // Blocking pop: waits up to 5s for a job, then loops again.
  // This keeps the process responsive to Ctrl+C instead of blocking forever.
  while (true) {
    try {
      const result = await redisClient.blPop(VOTE_QUEUE_KEY, 5);
      if (result) {
        const job = JSON.parse(result.element);
        await processJob(job);
      }
    } catch (err) {
      console.error("Worker loop error:", err.message);
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
  }
}

module.exports = { isValidJob };

// Only start the actual Redis/Postgres loop when this file is run directly
// (e.g. `node worker.js` / `npm start`) — not when it's require()'d by tests.
if (require.main === module) {
  main().catch((err) => {
    console.error("Worker crashed:", err);
    process.exit(1);
  });
}
