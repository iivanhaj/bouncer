-- Add a finished request's tokens to its key's monthly budget counter (DESIGN.md §2).
--
-- Only if the counter exists. If Redis lost it, INCRBY would create a new counter that
-- starts near zero and hides everything already spent this month. Instead, the next budget
-- check rebuilds the counter from the usage log in Postgres, which includes this request
-- once its row is written.
--
-- KEYS[1]  budget:{key_id}:{YYYY-MM}
-- ARGV[1]  tokens to add

if redis.call('EXISTS', KEYS[1]) == 1 then
  return redis.call('INCRBY', KEYS[1], ARGV[1])
end
return false
