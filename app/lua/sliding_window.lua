-- Sliding-window log rate limiter (DESIGN.md §2).
--
-- Redis runs a whole script before starting anything else, so "clean up, count, add" is one
-- indivisible step: two requests can never both see the same last free slot.
--
-- KEYS[1]  sorted set for one API key: member = request id, score = time in ms
-- ARGV[1]  window length in ms
-- ARGV[2]  limit (requests per window)
-- ARGV[3]  request id (unique, so two requests in the same millisecond are both counted)
--
-- Returns {allowed (1/0), remaining, retry_after_ms}

-- Redis's own clock, so every gateway replica agrees on the time.
local time = redis.call('TIME')
local now = tonumber(time[1]) * 1000 + math.floor(tonumber(time[2]) / 1000)
local window = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])

-- Forget requests that have slid out of the window.
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
local count = redis.call('ZCARD', KEYS[1])

if count < limit then
  redis.call('ZADD', KEYS[1], now, ARGV[3])
  -- The set cleans itself up once the key goes quiet for a whole window.
  redis.call('PEXPIRE', KEYS[1], window)
  return {1, limit - count - 1, 0}
end

-- Rejected requests are NOT recorded, so a client retrying in a loop doesn't keep
-- pushing its own window forward and lock itself out forever.
-- It can retry once the oldest request in the window slides out.
local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
return {0, 0, tonumber(oldest[2]) + window - now}
