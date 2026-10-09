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
// only counted where both were hot.
//
// Integer-only passes: each length's hot patterns get dense ids, and one id per
// code-point position (`uint16`; `uint32` when a length has more than 65,534
// hot patterns) holds the id of the hot window starting there (or NONE). A (k+1)-window is exactly the
// pair (its k-prefix's id, its k-suffix's id) — the two overlap in k−1 code
// points — so counting a length is an integer-keyed tally of adjacent id pairs,
// and assigning the next ids rewrites the array in place, left to right.
//
//   hot-frequency THRESHOLD MAX_CHARS THREADS [MAX_PATTERNS [SHORT_CHARS]] < rows > queries.jsonl
//
// MAX_CHARS 0: every length, until one has no hot pattern (the census is then
// complete: a pattern of any length is hot iff it is listed).
//
// SHORT_CHARS S > 0 also lists every pattern of at most S code points present
// in some name (at least one path), whatever its frequency: the short-literal
// domain, precomputed by cost (a substring index can't serve them) rather than
// by count. Only threshold-hot patterns seed longer lengths, so the pruning,
// and every pattern longer than S, are unchanged; the header declares S.
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
    out.resize(s.size() + 1);
    uint32_t n = 0;
    for (uint32_t i = 0; i < s.size(); ++i)
        if ((static_cast<unsigned char>(s[i]) & 0xC0) != 0x80) out[n++] = i;
    out[n++] = static_cast<uint32_t>(s.size());
    out.resize(n);
}

// Integer-keyed tally: key → path sum, each name adding at most once (`last`:
// the name index + 1 that last added — no per-name sort or set).
class Tally {
    struct Slot { uint64_t key; uint64_t sum; uint64_t last; };
    static constexpr uint64_t EMPTY = ~uint64_t(0);
    std::vector<Slot> slots;
    size_t used = 0;
    static uint64_t mix(uint64_t x) {
        x ^= x >> 33;
        x *= 0xff51afd7ed558ccdull;
        x ^= x >> 33;
        return x;
    }
    void grow() {
        std::vector<Slot> old(slots.size() ? slots.size() * 2 : 1 << 12, Slot{EMPTY, 0, 0});
        old.swap(slots);
        used = 0;
        for (const Slot& s : old) if (s.key != EMPTY) *find(s.key) = s, ++used;
    }
    Slot* find(uint64_t key) {
        const size_t mask = slots.size() - 1;
        for (size_t i = mix(key) & mask;; i = (i + 1) & mask)
            if (slots[i].key == key || slots[i].key == EMPTY) return &slots[i];
    }
public:
    Tally() { grow(); }
    void add_once(uint64_t key, uint64_t add, uint64_t name) {
        if ((used + 1) * 10 > slots.size() * 7) grow();
        Slot* s = find(key);
        if (s->key == EMPTY) { *s = Slot{key, add, name}; ++used; return; }
        if (s->last != name) { s->sum += add; s->last = name; }
    }
    void add(uint64_t key, uint64_t sum) {
        if ((used + 1) * 10 > slots.size() * 7) grow();
        Slot* s = find(key);
        if (s->key == EMPTY) { *s = Slot{key, sum, 0}; ++used; return; }
        s->sum += sum;
    }
    template <class F> void each(F f) const { for (const Slot& s : slots) if (s.key != EMPTY) f(s.key, s.sum); }
    size_t size() const { return used; }
};

// Read-only key → id (built single-threaded, probed concurrently).
class Ids {
    struct Slot { uint64_t key; uint32_t id; };
    static constexpr uint64_t EMPTY = ~uint64_t(0);
    std::vector<Slot> slots;
    static uint64_t mix(uint64_t x) {
        x ^= x >> 33;
        x *= 0xff51afd7ed558ccdull;
        x ^= x >> 33;
        return x;
    }
public:
    explicit Ids(size_t n) {
        size_t cap = 16;
        while (cap < n * 2 + 2) cap *= 2;
        slots.assign(cap, Slot{EMPTY, 0});
    }
    void put(uint64_t key, uint32_t id) {
        const size_t mask = slots.size() - 1;
        size_t i = mix(key) & mask;
        while (slots[i].key != EMPTY) i = (i + 1) & mask;
        slots[i] = Slot{key, id};
    }
    // NONE when absent.
    uint32_t get(uint64_t key, uint32_t none) const {
        const size_t mask = slots.size() - 1;
        for (size_t i = mix(key) & mask;; i = (i + 1) & mask) {
            if (slots[i].key == key) return slots[i].id;
            if (slots[i].key == EMPTY) return none;
        }
    }
};

// Well-formed UTF-8 (no overlongs, surrogates or code points past U+10FFFF) —
// the daily census refuses anything else, as `isValidUTF8` would.
static bool valid_utf8(std::string_view s) {
    const auto* p = reinterpret_cast<const unsigned char*>(s.data());
    const auto* end = p + s.size();
    while (p < end) {
        const unsigned c = *p;
        if (c < 0x80) { ++p; continue; }
        unsigned n, cp;
        if (c >= 0xC2 && c <= 0xDF) n = 1, cp = c & 0x1F;
        else if (c >= 0xE0 && c <= 0xEF) n = 2, cp = c & 0x0F;
        else if (c >= 0xF0 && c <= 0xF4) n = 3, cp = c & 0x07;
        else return false;
        if (end - p <= n) return false;
        for (unsigned i = 1; i <= n; ++i) {
            if ((p[i] & 0xC0) != 0x80) return false;
            cp = cp << 6 | (p[i] & 0x3F);
        }
        if ((n == 2 && (cp < 0x800 || (cp >= 0xD800 && cp <= 0xDFFF))) || (n == 3 && (cp < 0x10000 || cp > 0x10FFFF))) return false;
        p += n + 1;
    }
    return true;
}

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
        if (!valid_utf8(s)) fail("basename vocabulary contains invalid UTF-8");
        boundaries(s, b);
        v.offset.push_back(at + size);
        v.cp.push_back(v.cp.back() + (b.size() - 1));
        v.weight.push_back(c);
        v.paths += c;
    }
    return v;
}

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

// Raised when a length's hot patterns overflow the id width; the census reruns wider.
struct Widen {
    unsigned chars;
};

struct Census {
    std::vector<std::vector<std::string>> hot{{}};  // hot[k]: length k's patterns, in key order
    std::vector<std::vector<uint64_t>> sum{{}};
    uint64_t accepted = 0;
};

// The per-length passes with `Id`-wide window ids: `uint16_t` (2 bytes per
// code point) unless some length has more than 65,534 hot patterns, then
// `uint32_t` (twice the memory).
template <class Id>
static Census census(const Vocabulary& v, uint64_t threshold, unsigned max_chars, unsigned short_chars, unsigned threads, uint64_t max_patterns) {
    constexpr Id NONE = static_cast<Id>(~Id(0));
    constexpr unsigned SHIFT = 8 * sizeof(Id);
    constexpr uint64_t LOW = (uint64_t(1) << SHIFT) - 1;
    const size_t n = v.names();
    std::vector<Id> id(v.cp.back(), NONE);  // id[cp[j] + i]: the current length's hot window at i
    Census c;
    constexpr size_t CHUNK = 1 << 14;
    auto parallel = [&](auto body) {
        std::atomic<size_t> cursor{0};
        std::vector<std::thread> pool;
        for (unsigned t = 0; t < threads; ++t)
            pool.emplace_back([&, t] {
                for (size_t lo; (lo = cursor.fetch_add(CHUNK)) < n;) body(t, lo, std::min(n, lo + CHUNK));
            });
        for (auto& th : pool) th.join();
    };
    // A merged tally's listed keys, in key order: the hot ones (≥ threshold)
    // and, within the short domain, every one. Ids go to the patterns that seed
    // the next length — all of them below the short domain's last length,
    // else the hot ones — and come first in `c.hot[k]`, so an id indexes it.
    size_t seeded = 0;
    auto keep = [&](unsigned k, const std::vector<Tally>& local, auto spell) -> Ids {
        Tally merged;
        for (const Tally& t : local) t.each([&](uint64_t key, uint64_t sum) { merged.add(key, sum); });
        std::vector<std::pair<uint64_t, uint64_t>> seeds, extra;
        merged.each([&](uint64_t key, uint64_t sum) {
            if (sum >= threshold || k < short_chars) seeds.emplace_back(key, sum);
            else if (k <= short_chars) extra.emplace_back(key, sum);
        });
        std::sort(seeds.begin(), seeds.end());
        if (seeds.size() >= NONE) throw Widen{k};
        c.accepted += seeds.size() + extra.size();
        if (c.accepted > max_patterns) fail("accepted-pattern cap exceeded at length " + std::to_string(k));
        Ids ids(seeds.size());
        c.hot.emplace_back();
        c.sum.emplace_back();
        for (uint32_t x = 0; x < seeds.size(); ++x) {
            ids.put(seeds[x].first, x);
            c.hot[k].push_back(spell(seeds[x].first));
            c.sum[k].push_back(seeds[x].second);
        }
        for (const auto& [key, sum] : extra) {
            c.hot[k].push_back(spell(key));
            c.sum[k].push_back(sum);
        }
        seeded = seeds.size();
        std::fprintf(stderr, "{\"stage\":\"hot-substrings\",\"id_bytes\":%zu,\"chars\":%u,\"candidates\":%zu,\"hot_patterns\":%zu,", sizeof(Id), k, merged.size(), seeds.size() + extra.size());
        return ids;
    };

    for (unsigned k = 1; !max_chars || k <= max_chars; ++k) {
        const double start = now();
        std::vector<Tally> local(threads);
        // Count: length 1 by each code point's bytes (packed, ≤ 4); length k > 1
        // by the adjacent (k−1)-ids (prefix, suffix), both hot.
        parallel([&](unsigned t, size_t lo, size_t hi) {
            std::vector<uint32_t> b;
            for (size_t j = lo; j < hi; ++j) {
                const uint64_t base = v.cp[j];
                const uint32_t len = static_cast<uint32_t>(v.cp[j + 1] - base);
                if (len < k) continue;
                if (k == 1) {
                    const std::string_view s = v.name(j);
                    boundaries(s, b);
                    for (uint32_t i = 0; i < len; ++i) {
                        uint64_t key = 0;
                        std::memcpy(&key, s.data() + b[i], b[i + 1] - b[i]);
                        local[t].add_once(key, v.weight[j], j + 1);
                    }
                    continue;
                }
                const Id* w = id.data() + base;
                for (uint32_t i = 0; i + k <= len; ++i)
                    if (w[i] != NONE && w[i + 1] != NONE) local[t].add_once(uint64_t(w[i]) << SHIFT | w[i + 1], v.weight[j], j + 1);
            }
        });
        const double counted = now();
        // A (k+1)-pattern's spelling: its prefix, then its suffix's last code point.
        auto last_cp = [](const std::string& x) {
            size_t i = x.size() - 1;
            while (i > 0 && (static_cast<unsigned char>(x[i]) & 0xC0) == 0x80) --i;
            return x.substr(i);
        };
        Ids ids = k == 1
            ? keep(k, local, [](uint64_t key) { return std::string(reinterpret_cast<const char*>(&key), strnlen(reinterpret_cast<const char*>(&key), 4)); })
            : keep(k, local, [&](uint64_t key) { return c.hot[k - 1][key >> SHIFT] + last_cp(c.hot[k - 1][key & LOW]); });
        const bool last = (seeded == 0 && k >= short_chars) || k == max_chars;
        // Assign: the ids of this length's hot windows, in place.
        if (!last) {
            parallel([&](unsigned, size_t lo, size_t hi) {
                std::vector<uint32_t> b;
                for (size_t j = lo; j < hi; ++j) {
                    const uint64_t base = v.cp[j];
                    const uint32_t len = static_cast<uint32_t>(v.cp[j + 1] - base);
                    Id* w = id.data() + base;
                    if (len < k) continue;
                    if (k == 1) {
                        const std::string_view s = v.name(j);
                        boundaries(s, b);
                        for (uint32_t i = 0; i < len; ++i) {
                            uint64_t key = 0;
                            std::memcpy(&key, s.data() + b[i], b[i + 1] - b[i]);
                            w[i] = static_cast<Id>(ids.get(key, NONE));
                        }
                        continue;
                    }
                    for (uint32_t i = 0; i + k <= len; ++i)
                        w[i] = w[i] != NONE && w[i + 1] != NONE ? static_cast<Id>(ids.get(uint64_t(w[i]) << SHIFT | w[i + 1], NONE)) : NONE;
                    w[len - k + 1] = NONE;  // one fewer window at this length
                }
            });
        }
        std::fprintf(stderr, "\"count_s\":%.3f,\"assign_s\":%.3f}\n", counted - start, now() - counted);
        if (last) break;
    }
    return c;
}

int main(int argc, char** argv) try {
    if (argc < 4 || argc > 6) fail("usage: hot-frequency THRESHOLD MAX_CHARS THREADS [MAX_PATTERNS [SHORT_CHARS]] < rows");
    const uint64_t threshold = std::stoull(argv[1]);
    const unsigned max_chars = static_cast<unsigned>(std::stoul(argv[2]));
    const unsigned threads = static_cast<unsigned>(std::stoul(argv[3]));
    const uint64_t max_patterns = argc >= 5 ? std::stoull(argv[4]) : 500000;
    const unsigned short_chars = argc == 6 ? static_cast<unsigned>(std::stoul(argv[5])) : 0;
    if (!threshold || !threads || (max_chars && short_chars > max_chars)) fail("bad arguments");

    double t0 = now();
    const Vocabulary v = read_vocabulary();
    std::fprintf(stderr, "{\"stage\":\"read\",\"distinct_names\":%zu,\"paths\":%llu,\"bytes\":%zu,\"code_points\":%llu,\"elapsed_s\":%.3f}\n",
                 v.names(), (unsigned long long)v.paths, v.text.size(), (unsigned long long)v.cp.back(), now() - t0);

    Census c;
    try {
        c = census<uint16_t>(v, threshold, max_chars, short_chars, threads, max_patterns);
    } catch (const Widen& w) {
        std::fprintf(stderr, "{\"stage\":\"widen\",\"chars\":%u,\"id_bytes\":4}\n", w.chars);
        c = census<uint32_t>(v, threshold, max_chars, short_chars, threads, max_patterns);
    }

    std::string out = "{\"schema\": \"hot-frequency-queries-v1\", \"engine\": \"native\", \"threshold_paths\": " + std::to_string(threshold) +
                      ", \"max_chars\": " + (max_chars ? std::to_string(max_chars) : "null") +
                      (short_chars ? ", \"short_chars\": " + std::to_string(short_chars) : "") + "}\n";
    for (size_t k = 1; k < c.hot.size(); ++k) {
        std::vector<size_t> order(c.hot[k].size());
        for (size_t i = 0; i < order.size(); ++i) order[i] = i;
        std::sort(order.begin(), order.end(), [&](size_t a, size_t b) { return c.hot[k][a] < c.hot[k][b]; });
        for (size_t i : order) {
            out += "{\"chars\":" + std::to_string(k) + ",\"pattern\":";
            json_string(out, c.hot[k][i]);
            out += ",\"direct_matching_paths\":" + std::to_string(c.sum[k][i]) + "}\n";
        }
    }
    out += "{\"complete\": true, \"patterns\": " + std::to_string(c.accepted) + "}\n";
    if (std::fwrite(out.data(), 1, out.size(), stdout) != out.size() || std::fflush(stdout)) fail("output write failed");
    std::fprintf(stderr, "{\"stage\":\"done\",\"patterns\":%llu,\"elapsed_s\":%.3f}\n", (unsigned long long)c.accepted, now() - t0);
    return 0;
} catch (const std::exception& e) {
    std::fprintf(stderr, "hot-frequency: %s\n", e.what());
    return 1;
}
