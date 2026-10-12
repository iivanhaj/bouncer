-- Concurrency slot acquisition (DESIGN.md §2).
--
-- One sorted set per API key holds the requests currently in progress:
-- member = request id, score = start time in ms. Like the rate limiter, cleanup + count + add
-- run as one indivisible step.
--
-- KEYS[1]  sorted set of in-progress requests for one API key
-- ARGV[1]  stale age in ms: entries older than this belong to requests that can't still be running
-- ARGV[2]  limit (max requests in progress)
-- ARGV[3]  request id
--
-- Returns {acquired (1/0), in_progress}

local time = redis.call('TIME')
local now = tonumber(time[1]) * 1000 + math.floor(tonumber(time[2]) / 1000)
local stale = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])

-- If the gateway crashed mid-request, its `finally` never ran and the slot was never released.
-- Every request is cut off by the total timeout, which is shorter than the stale age, so any
-- entry this old is a leftover. Removing it here is what makes leaked slots heal themselves
-- (a plain INCR/DECR counter would stay wrong forever).
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - stale)
local count = redis.call('ZCARD', KEYS[1])

if count < limit then
  redis.call('ZADD', KEYS[1], now, ARGV[3])
  redis.call('PEXPIRE', KEYS[1], stale)
  return {1, count + 1}
end
return {0, count}
