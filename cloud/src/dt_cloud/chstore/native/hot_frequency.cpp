// C++17 exact hot-substring census over a weighted basename vocabulary.
//
// stdin: RowBinary rows `(l String, c UInt64)` — distinct lowercase basenames
// and their direct path counts (`hot_frequency_daily.weighted_select`'s shape).
// stdout: the `hot-frequency-queries-v1` JSONL `ch-hot-frequency-census -q`
// writes: a header, `{"chars","pattern","direct_matching_paths"}` per hot
// pattern (by length, then bytewise pattern order — ClickHouse's `ORDER BY
// gram`), and a completion footer. A pattern is hot iff the paths whose
// basename contains it (as `ngrams(l, chars)` would produce it: UTF-8 code
// points, counted once per name) sum to at least the threshold.
//
// Exactness of the pruning: a name containing a k-gram contains its
// (k−1)-prefix and (k−1)-suffix, so both are at least as frequent; a k-gram is
// only counted where both were hot. The alive bitset marks, per code-point
// position, whether the current length's window starting there is hot, so
// each length touches only extensions of hot windows.
//
//   hot-frequency THRESHOLD MAX_CHARS THREADS [MAX_PATTERNS] < rows > queries.jsonl
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <iostream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <vector>

[[noreturn]] static void fail(const std::string& message) { throw std::runtime_error(message); }

static double now() {
    using namespace std::chrono;
    return duration<double>(steady_clock::now().time_since_epoch()).count();
}

class Input {
    std::vector<unsigned char> buffer = std::vector<unsigned char>(16 << 20);
    size_t pos = 0, end = 0;
    bool fill() {
        end = std::fread(buffer.data(), 1, buffer.size(), stdin);
        pos = 0;
        if (!end && std::ferror(stdin)) fail("input read failed");
        return end != 0;
    }
public:
    bool eof() { return pos == end && !fill(); }
    uint8_t byte() {
        if (pos == end && !fill()) fail("truncated input");
        return buffer[pos++];
    }
    uint64_t varuint() {
        uint64_t value = 0;
        for (unsigned i = 0; i < 10; ++i) {
            const uint8_t part = byte();
            value |= uint64_t(part & 127) << (i * 7);
            if (!(part & 128)) return value;
        }
        fail("invalid RowBinary string length");
    }
    void bytes(char* out, size_t n) {
        while (n) {
            if (pos == end && !fill()) fail("truncated input");
            const size_t take = std::min(n, end - pos);
            std::memcpy(out, buffer.data() + pos, take);
            pos += take, out += take, n -= take;
        }
    }
    uint64_t u64() {
        uint64_t value = 0;
        for (unsigned i = 0; i < 8; ++i) value |= uint64_t(byte()) << (i * 8);
        return value;
    }
};

// Code-point boundaries of one name, as ClickHouse's UTF-8 functions see them:
// a lead byte starts a code point, continuation bytes (10xxxxxx) don't.
static void boundaries(std::string_view s, std::vector<uint32_t>& out) {
    out.clear();
    for (uint32_t i = 0; i < s.size(); ++i)
        if ((static_cast<unsigned char>(s[i]) & 0xC0) != 0x80) out.push_back(i);
    out.push_back(static_cast<uint32_t>(s.size()));
}

static uint64_t hash_bytes(std::string_view s) {
    uint64_t h = 1469598103934665603ull ^ (s.size() * 0x9E3779B97F4A7C15ull);
    size_t i = 0;
    for (; i + 8 <= s.size(); i += 8) {
        uint64_t w;
        std::memcpy(&w, s.data() + i, 8);
        h = (h ^ w) * 0x100000001B3ull;
        h ^= h >> 29;
    }
    uint64_t tail = 0;
    std::memcpy(&tail, s.data() + i, s.size() - i);
    h = (h ^ tail) * 0x100000001B3ull;
    h ^= h >> 32;
    h *= 0xD6E8FEB86659FD93ull;
    h ^= h >> 32;
    return h;
}

// Open-addressing map from a window (a view into the name buffer) to its path sum.
class Counts {
    struct Slot { uint64_t hash; const char* data; uint32_t size; uint64_t sum; };
    std::vector<Slot> slots;
    size_t used = 0;
    void grow() {
        std::vector<Slot> old(slots.size() ? slots.size() * 2 : 1 << 16, Slot{0, nullptr, 0, 0});
        old.swap(slots);
        used = 0;
        for (const Slot& s : old) if (s.data) insert(s.hash, s.data, s.size, s.sum);
    }
public:
    Counts() { grow(); }
    void insert(uint64_t hash, const char* data, uint32_t size, uint64_t add) {
        if ((used + 1) * 10 > slots.size() * 7) grow();
        const size_t mask = slots.size() - 1;
        for (size_t i = hash & mask;; i = (i + 1) & mask) {
            Slot& s = slots[i];
            if (!s.data) {
                s = Slot{hash, data, size, add};
                ++used;
                return;
            }
            if (s.hash == hash && s.size == size && std::memcmp(s.data, data, size) == 0) {
                s.sum += add;
                return;
            }
        }
    }
    bool contains(uint64_t hash, const char* data, uint32_t size) const {
        const size_t mask = slots.size() - 1;
        for (size_t i = hash & mask;; i = (i + 1) & mask) {
            const Slot& s = slots[i];
            if (!s.data) return false;
            if (s.hash == hash && s.size == size && std::memcmp(s.data, data, size) == 0) return true;
        }
    }
    template <class F> void each(F f) const { for (const Slot& s : slots) if (s.data) f(s.hash, s.data, s.size, s.sum); }
    size_t size() const { return used; }
};

struct Vocabulary {
    std::vector<char> text;
    std::vector<uint64_t> offset{0};  // byte offset of each name; one past the last
    std::vector<uint64_t> cp{0};      // code-point offset of each name; one past the last
    std::vector<uint64_t> weight;
    uint64_t paths = 0;
    size_t names() const { return weight.size(); }
    std::string_view name(size_t j) const { return {text.data() + offset[j], offset[j + 1] - offset[j]}; }
};

static Vocabulary read_vocabulary() {
    Input in;
    Vocabulary v;
    std::vector<uint32_t> b;
    while (!in.eof()) {
        const uint64_t size = in.varuint();
        if (size > (1u << 20)) fail("basename longer than 1 MiB");
        const size_t at = v.text.size();
        v.text.resize(at + size);
        in.bytes(v.text.data() + at, size);
        const uint64_t c = in.u64();
        const std::string_view s(v.text.data() + at, size);
        if (s.find('/') != std::string_view::npos) fail("basename vocabulary contains a path separator");
        if (s.find('\0') != std::string_view::npos) fail("basename vocabulary contains NUL");
        boundaries(s, b);
        v.offset.push_back(at + size);
        v.cp.push_back(v.cp.back() + (b.size() - 1));
        v.weight.push_back(c);
        v.paths += c;
    }
    return v;
}

class Bits {
    std::vector<uint64_t> words;
public:
    explicit Bits(uint64_t n) : words((n + 64) / 64) {}
    bool get(uint64_t i) const { return words[i >> 6] >> (i & 63) & 1; }
    // Concurrent writers touch different names, but adjacent names share words.
    void set(uint64_t i) { __atomic_fetch_or(&words[i >> 6], uint64_t(1) << (i & 63), __ATOMIC_RELAXED); }
};

static void json_string(std::string& out, std::string_view s) {
    out.push_back('"');
    for (unsigned char ch : s) {
        switch (ch) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            case '\b': out += "\\b"; break;
            case '\f': out += "\\f"; break;
            default:
                if (ch < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof buf, "\\u%04x", ch);
                    out += buf;
                } else {
                    out.push_back(static_cast<char>(ch));
                }
        }
    }
    out.push_back('"');
}

int main(int argc, char** argv) try {
    if (argc < 4 || argc > 5) fail("usage: hot-frequency THRESHOLD MAX_CHARS THREADS [MAX_PATTERNS] < rows");
    const uint64_t threshold = std::stoull(argv[1]);
    const unsigned max_chars = static_cast<unsigned>(std::stoul(argv[2]));
    const unsigned threads = static_cast<unsigned>(std::stoul(argv[3]));
    const uint64_t max_patterns = argc == 5 ? std::stoull(argv[4]) : 500000;
    if (!threshold || !max_chars || max_chars > 32 || !threads) fail("bad arguments");

    double t0 = now();
    const Vocabulary v = read_vocabulary();
    const size_t n = v.names();
    std::fprintf(stderr, "{\"stage\":\"read\",\"distinct_names\":%zu,\"paths\":%llu,\"bytes\":%zu,\"code_points\":%llu,\"elapsed_s\":%.3f}\n",
                 n, (unsigned long long)v.paths, v.text.size(), (unsigned long long)v.cp.back(), now() - t0);

    Bits alive(v.cp.back());  // alive[cp[j] + i]: the previous length's window at i is hot
    std::vector<std::vector<std::string_view>> hot(max_chars + 1);
    std::vector<std::vector<uint64_t>> hot_sum(max_chars + 1);
    uint64_t accepted = 0;
    Counts previous;

    for (unsigned k = 1; k <= max_chars; ++k) {
        const double start = now();
        if (k > 1 && hot[k - 1].empty()) break;
        // Pass 1: count each candidate window once per name, per thread.
        std::vector<Counts> local(threads);
        std::atomic<size_t> next{0};
        constexpr size_t CHUNK = 1 << 14;
        auto count = [&](unsigned t) {
            std::vector<uint32_t> b;
            std::vector<std::pair<uint64_t, std::string_view>> seen;
            Counts& out = local[t];
            for (size_t lo; (lo = next.fetch_add(CHUNK)) < n;) {
                for (size_t j = lo, hi = std::min(n, lo + CHUNK); j < hi; ++j) {
                    const std::string_view s = v.name(j);
                    boundaries(s, b);
                    const uint32_t len = static_cast<uint32_t>(b.size() - 1);
                    if (len < k) continue;
                    seen.clear();
                    const uint64_t base = v.cp[j];
                    for (uint32_t i = 0; i + k <= len; ++i) {
                        if (k > 1 && !(alive.get(base + i) && alive.get(base + i + 1))) continue;
                        const std::string_view w = s.substr(b[i], b[i + k] - b[i]);
                        seen.emplace_back(hash_bytes(w), w);
                    }
                    if (seen.empty()) continue;
                    std::sort(seen.begin(), seen.end());
                    for (size_t x = 0; x < seen.size(); ++x) {
                        if (x && seen[x] == seen[x - 1]) continue;
                        out.insert(seen[x].first, seen[x].second.data(), static_cast<uint32_t>(seen[x].second.size()), v.weight[j]);
                    }
                }
            }
        };
        {
            std::vector<std::thread> pool;
            for (unsigned t = 0; t < threads; ++t) pool.emplace_back(count, t);
            for (auto& th : pool) th.join();
        }
        Counts merged;
        for (const Counts& c : local) c.each([&](uint64_t h, const char* d, uint32_t sz, uint64_t sum) { merged.insert(h, d, sz, sum); });
        const size_t candidates = merged.size();
        local.clear();
        Counts current;
        merged.each([&](uint64_t h, const char* d, uint32_t sz, uint64_t sum) {
            if (sum < threshold) return;
            current.insert(h, d, sz, sum);
            hot[k].emplace_back(d, sz);
            hot_sum[k].push_back(sum);
        });
        accepted += hot[k].size();
        if (accepted > max_patterns) fail("accepted-pattern cap exceeded at length " + std::to_string(k));
        const double counted = now();
        // Pass 2: the next length's alive bits — this length's hot windows.
        if (k < max_chars && !hot[k].empty()) {
            Bits next_alive(v.cp.back());
            std::atomic<size_t> cursor{0};
            auto mark = [&]() {
                std::vector<uint32_t> b;
                for (size_t lo; (lo = cursor.fetch_add(CHUNK)) < n;) {
                    for (size_t j = lo, hi = std::min(n, lo + CHUNK); j < hi; ++j) {
                        const std::string_view s = v.name(j);
                        boundaries(s, b);
                        const uint32_t len = static_cast<uint32_t>(b.size() - 1);
                        const uint64_t base = v.cp[j];
                        for (uint32_t i = 0; i + k <= len; ++i) {
                            if (k > 1 && !(alive.get(base + i) && alive.get(base + i + 1))) continue;
                            const std::string_view w = s.substr(b[i], b[i + k] - b[i]);
                            if (current.contains(hash_bytes(w), w.data(), static_cast<uint32_t>(w.size()))) next_alive.set(base + i);
                        }
                    }
                }
            };
            std::vector<std::thread> pool;
            for (unsigned t = 0; t < threads; ++t) pool.emplace_back(mark);
            for (auto& th : pool) th.join();
            alive = std::move(next_alive);
        }
        std::fprintf(stderr, "{\"stage\":\"hot-substrings\",\"chars\":%u,\"candidates\":%zu,\"hot_patterns\":%zu,\"count_s\":%.3f,\"mark_s\":%.3f}\n",
                     k, candidates, hot[k].size(), counted - start, now() - counted);
    }

    std::string out = "{\"schema\": \"hot-frequency-queries-v1\", \"engine\": \"native\", \"threshold_paths\": " + std::to_string(threshold) +
                      ", \"max_chars\": " + std::to_string(max_chars) + "}\n";
    for (unsigned k = 1; k <= max_chars; ++k) {
        std::vector<size_t> order(hot[k].size());
        for (size_t i = 0; i < order.size(); ++i) order[i] = i;
        std::sort(order.begin(), order.end(), [&](size_t a, size_t b) { return hot[k][a] < hot[k][b]; });
        for (size_t i : order) {
            out += "{\"chars\":" + std::to_string(k) + ",\"pattern\":";
            json_string(out, hot[k][i]);
            out += ",\"direct_matching_paths\":" + std::to_string(hot_sum[k][i]) + "}\n";
        }
    }
    out += "{\"complete\": true, \"patterns\": " + std::to_string(accepted) + "}\n";
    if (std::fwrite(out.data(), 1, out.size(), stdout) != out.size() || std::fflush(stdout)) fail("output write failed");
    std::fprintf(stderr, "{\"stage\":\"done\",\"patterns\":%llu,\"elapsed_s\":%.3f}\n", (unsigned long long)accepted, now() - t0);
    return 0;
} catch (const std::exception& e) {
    std::fprintf(stderr, "hot-frequency: %s\n", e.what());
    return 1;
}
