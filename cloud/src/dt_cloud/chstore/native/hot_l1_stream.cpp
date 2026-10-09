// C++17 offline first-hit DFS aggregation. Input strings are already normalized.
// No output is accepted until the declared node count and final EOF are verified.
#include <algorithm>
#include <array>
#include <cstdint>
#include <cstdio>
#include <iostream>
#include <limits>
#include <queue>
#include <stdexcept>
#include <string>
#include <sys/resource.h>
#include <unordered_set>
#include <utility>
#include <vector>

using U128 = unsigned __int128;
constexpr uint32_t NONE = std::numeric_limits<uint32_t>::max();

[[noreturn]] static void fail(const char* message) { throw std::runtime_error(message); }

class Input {
    std::vector<unsigned char> buffer = std::vector<unsigned char>(8 << 20);
    size_t pos = 0, end = 0;
public:
    int byte() {
        if (pos == end) {
            end = std::fread(buffer.data(), 1, buffer.size(), stdin);
            pos = 0;
            if (!end) {
                if (std::ferror(stdin)) fail("input read failed");
                return -1;
            }
        }
        return buffer[pos++];
    }
    uint8_t required() {
        const int value = byte();
        if (value < 0) fail("truncated input");
        return static_cast<uint8_t>(value);
    }
    uint64_t fixed(unsigned bytes) {
        uint64_t value = 0;
        for (unsigned i = 0; i < bytes; ++i) value |= uint64_t(required()) << (i * 8);
        return value;
    }
    uint64_t varuint() {
        uint64_t value = 0;
        for (unsigned i = 0; i < 10; ++i) {
            const uint8_t part = required();
            if (i == 9 && part > 1) fail("invalid RowBinary string length");
            value |= uint64_t(part & 127) << (i * 7);
            if (!(part & 128)) return value;
        }
        fail("invalid RowBinary string length");
    }
    std::string string() {
        const uint64_t size = varuint();
        // Bound a malformed length before allocating; this is a protocol limit,
        // not a query/node-count limit. Real object-store names are much smaller.
        if (size > (64U << 20)) fail("RowBinary string exceeds 64 MiB protocol limit");
        std::string value(static_cast<size_t>(size), '\0');
        size_t copied = 0;
        while (copied < size) {
            if (pos == end) {
                value[copied++] = static_cast<char>(required());
                continue;
            }
            const size_t count = std::min(static_cast<size_t>(size) - copied, end - pos);
            std::copy_n(buffer.data() + pos, count, value.begin() + copied);
            pos += count;
            copied += count;
        }
        return value;
    }
};

static bool valid_utf8(const std::string& text) {
    for (size_t i = 0; i < text.size();) {
        const uint8_t first = static_cast<uint8_t>(text[i++]);
        if (first < 128) continue;
        unsigned extra;
        uint32_t code, minimum;
        if (first >= 0xC2 && first <= 0xDF) { extra = 1; code = first & 31; minimum = 0x80; }
        else if (first >= 0xE0 && first <= 0xEF) { extra = 2; code = first & 15; minimum = 0x800; }
        else if (first >= 0xF0 && first <= 0xF4) { extra = 3; code = first & 7; minimum = 0x10000; }
        else return false;
        if (extra > text.size() - i) return false;
        for (unsigned j = 0; j < extra; ++j) {
            const uint8_t next = static_cast<uint8_t>(text[i++]);
            if ((next & 0xC0) != 0x80) return false;
            code = (code << 6) | (next & 63);
        }
        if (code < minimum || code > 0x10FFFF || (code >= 0xD800 && code <= 0xDFFF)) return false;
    }
    return true;
}

struct State {
    std::vector<std::pair<uint8_t, uint32_t>> edges;
    uint32_t fail = 0, output = NONE, terminal = NONE;
};

class Automaton {
    std::vector<State> states = std::vector<State>(1);
    std::array<uint32_t, 256> root{};
    uint32_t edge(uint32_t state, uint8_t character) const {
        if (!state) return root[character];
        const auto& edges = states[state].edges;
        auto it = std::lower_bound(edges.begin(), edges.end(), character,
            [](const auto& item, uint8_t value) { return item.first < value; });
        return it != edges.end() && it->first == character ? it->second : NONE;
    }
public:
    void add(const std::string& text, uint32_t query) {
        uint32_t state = 0;
        for (uint8_t character : text) {
            uint32_t child = NONE;
            for (const auto& item : states[state].edges)
                if (item.first == character) { child = item.second; break; }
            if (child == NONE) {
                if (states.size() >= NONE) fail("automaton state count exceeds UInt32");
                child = static_cast<uint32_t>(states.size());
                states.emplace_back();
                states[state].edges.emplace_back(character, child);
            }
            state = child;
        }
        states[state].terminal = query;
    }
    void finish() {
        root.fill(NONE);
        for (auto& state : states) std::sort(state.edges.begin(), state.edges.end());
        std::queue<uint32_t> queue;
        for (const auto& item : states[0].edges) { root[item.first] = item.second; queue.push(item.second); }
        while (!queue.empty()) {
            const uint32_t state = queue.front();
            queue.pop();
            for (const auto& item : states[state].edges) {
                uint32_t fallback = states[state].fail;
                uint32_t child = edge(fallback, item.first);
                while (fallback && child == NONE) { fallback = states[fallback].fail; child = edge(fallback, item.first); }
                const uint32_t failed = child == NONE ? 0 : child;
                states[item.second].fail = failed;
                states[item.second].output = states[failed].terminal == NONE ? states[failed].output : failed;
                queue.push(item.second);
            }
        }
    }
    template<typename Hit> void scan(const std::string& text, Hit hit) const {
        uint32_t state = 0;
        for (uint8_t character : text) {
            uint32_t child = edge(state, character);
            while (state && child == NONE) { state = states[state].fail; child = edge(state, character); }
            state = child == NONE ? 0 : child;
            for (uint32_t output = state; output != NONE; output = states[output].output)
                if (states[output].terminal != NONE) hit(states[output].terminal);
        }
    }
};

struct Bounds { uint64_t pre, post; };
struct Weights { U128 b = 0, o = 0; };
struct Frame {
    uint64_t post, b, o;
    Weights children;
    std::vector<uint32_t> activated;
};

static std::string decimal(U128 value) {
    if (!value) return "0";
    std::string result;
    while (value) { result.push_back('0' + value % 10); value /= 10; }
    std::reverse(result.begin(), result.end());
    return result;
}

static void run() {
    Input input;
    for (char expected : std::string("HL1DFS01"))
        if (input.required() != expected) fail("invalid protocol magic");
    const uint64_t expected_nodes = input.fixed(8);
    const uint32_t queries = static_cast<uint32_t>(input.fixed(4));
    const uint8_t bucket_count = input.required();
    if (!expected_nodes || !queries) fail("protocol requires a root and nonempty query registry");
    if (bucket_count < 1 || bucket_count > 6) fail("protocol requires one to six buckets");
    std::vector<Bounds> bounds;
    for (unsigned i = 0; i < bucket_count; ++i) {
        Bounds bucket{input.fixed(8), input.fixed(8)};
        if (bucket.pre > bucket.post || (!i && bucket.pre != 1) ||
            (i && (bounds.back().post == UINT64_MAX || bucket.pre != bounds.back().post + 1)))
            fail("bucket intervals must partition the ordered global domain");
        bounds.push_back(bucket);
    }
    Automaton automaton;
    {
        std::unordered_set<std::string> registered;
        for (uint32_t q = 0; q < queries; ++q) {
            std::string pattern = input.string();
            if (pattern.empty() || pattern.find('/') != std::string::npos || pattern.find('\0') != std::string::npos || !valid_utf8(pattern))
                fail("queries must be nonempty valid UTF-8 NUL/slash-free literals");
            if (!registered.insert(pattern).second) fail("query literals must be unique");
            automaton.add(pattern, q);
        }
    }
    automaton.finish();
    if (queries > std::numeric_limits<size_t>::max() / bucket_count / sizeof(Weights)) fail("matrix allocation overflows");
    std::vector<Weights> matrix(static_cast<size_t>(queries) * bucket_count);
    std::vector<uint8_t> active(queries, 0), present(bucket_count, 0);
    std::vector<uint64_t> seen(queries, 0);
    std::vector<Weights> ordinary(bucket_count);
    std::vector<Frame> stack;
    Weights root;
    uint64_t previous = 0, active_count = 0, peak_active = 0, peak_stack = 0;
    unsigned bucket_index = 0;
    auto pop = [&]() {
        const Frame& frame = stack.back();
        if (frame.children.b > frame.b || frame.children.o > frame.o) fail("child rollups exceed their parent");
        for (uint32_t q : frame.activated) active[q] = 0;
        active_count -= frame.activated.size();
        stack.pop_back();
    };
    for (uint64_t i = 0; i < expected_nodes; ++i) {
        const uint64_t pre = input.fixed(8), post = input.fixed(8), b = input.fixed(8), o = input.fixed(8);
        const std::string name = input.string();
        if (!valid_utf8(name) || name.find('/') != std::string::npos) fail("node names must be valid UTF-8 slash-free strings");
        if (pre > post || (i && pre <= previous)) fail("node order or interval bounds are invalid");
        if (post > bounds.back().post) fail("node lies outside the global domain");
        previous = pre;
        if (!i) {
            if (pre != 0 || post != bounds.back().post || !name.empty()) fail("stream must start with the empty-name complete global root");
            root = {b, o};
            stack.push_back({post, b, o, {}, {}});
            peak_stack = 1;
            continue;
        }
        while (!stack.empty() && stack.back().post < pre) pop();
        if (stack.empty() || post > stack.back().post) fail("node intervals cross rather than nest");
        stack.back().children.b += b;
        stack.back().children.o += o;
        while (bucket_index < bucket_count && pre > bounds[bucket_index].post) ++bucket_index;
        if (bucket_index == bucket_count || post > bounds[bucket_index].post) fail("node interval crosses bucket boundaries");
        if (pre == bounds[bucket_index].pre) {
            if (post != bounds[bucket_index].post) fail("bucket node differs from its frozen bounds");
            ordinary[bucket_index] = {b, o};
            present[bucket_index] = 1;
        } else if (!present[bucket_index]) fail("descendant has no present bucket root");
        const bool leaf = pre == post;
        std::vector<uint32_t> activated;
        uint64_t new_hits = 0;
        automaton.scan(name, [&](uint32_t q) {
            if (seen[q] == i + 1) return;
            seen[q] = i + 1;
            if (active[q]) return;
            Weights& total = matrix[static_cast<size_t>(q) * bucket_count + bucket_index];
            total.b += b;
            total.o += o;
            ++new_hits;
            if (!leaf) { active[q] = 1; activated.push_back(q); }
        });
        peak_active = std::max(peak_active, active_count + new_hits);
        if (!leaf) {
            active_count += new_hits;
            stack.push_back({post, b, o, {}, std::move(activated)});
            peak_stack = std::max(peak_stack, static_cast<uint64_t>(stack.size()));
        }
    }
    if (input.byte() != -1) fail("trailing input after declared node count");
    while (!stack.empty()) pop();
    Weights sum;
    for (const auto& bucket : ordinary) { sum.b += bucket.b; sum.o += bucket.o; }
    if (sum.b != root.b || sum.o != root.o) fail("bucket rollups disagree with the global root");
    struct rusage usage{};
    if (getrusage(RUSAGE_SELF, &usage) != 0) fail("getrusage failed");
#ifdef __APPLE__
    const uint64_t rss = usage.ru_maxrss;
#else
    const uint64_t rss = static_cast<uint64_t>(usage.ru_maxrss) * 1024;
#endif
    std::cout << "{\"schema\":\"hot-l1-native-stream-v1\",\"exact\":true,\"incremental\":false,\"levels\":1,\"nodes_read\":"
              << expected_nodes << ",\"registered_predicates\":" << queries << ",\"peak_stack\":" << peak_stack
              << ",\"peak_active\":" << peak_active << ",\"native_peak_rss_bytes\":" << rss << ",\"matrix\":[";
    for (uint32_t q = 0; q < queries; ++q) {
        if (q) std::cout << ',';
        std::cout << "{\"predicate_id\":" << uint64_t(q) + 1 << ",\"buckets\":[";
        for (unsigned j = 0; j < bucket_count; ++j) {
            if (j) std::cout << ',';
            const Weights& value = matrix[static_cast<size_t>(q) * bucket_count + j];
            std::cout << "[\"" << decimal(value.b) << "\",\"" << decimal(value.o) << "\"]";
        }
        std::cout << "]}";
    }
    std::cout << "]}\n";
    if (!std::cout) fail("output write failed");
}

static void prefix_audit() {
    Input input;
    for (char expected : std::string("HL1PRE01"))
        if (input.required() != expected) fail("invalid prefix protocol magic");
    const uint64_t nodes = input.fixed(8);
    const unsigned count = input.required();
    if (!nodes || count < 1 || count > 6) fail("prefix protocol requires a root and one to six buckets");
    std::vector<Bounds> bounds;
    for (unsigned i = 0; i < count; ++i) {
        Bounds bucket{input.fixed(8), input.fixed(8)};
        if (bucket.pre > bucket.post || (!i && bucket.pre != 1) ||
            (i && (bounds.back().post == UINT64_MAX || bucket.pre != bounds.back().post + 1)))
            fail("prefix bucket intervals must partition the ordered global domain");
        bounds.push_back(bucket);
    }
    struct Ancestor { uint64_t post; unsigned depth; };
    std::vector<Ancestor> stack;
    uint64_t previous = 0, peak = 0;
    unsigned bucket_index = 0;
    for (uint64_t i = 0; i < nodes; ++i) {
        const uint64_t pre = input.fixed(8), post = input.fixed(8);
        const unsigned depth = input.required();
        if (pre > post || (i && pre <= previous)) fail("prefix node order or interval bounds are invalid");
        if (post > bounds.back().post) fail("prefix node lies outside the global domain");
        previous = pre;
        if (!i) {
            if (pre != 0 || post != bounds.back().post || depth != 0) fail("prefix stream requires the complete depth-zero global root");
            stack.push_back({post, depth});
            peak = 1;
            continue;
        }
        while (!stack.empty() && stack.back().post < pre) stack.pop_back();
        if (stack.empty() || post > stack.back().post) fail("prefix node intervals cross rather than nest");
        if (depth != stack.back().depth + 1) fail("prefix node lacks its present immediate parent");
        while (bucket_index < count && pre > bounds[bucket_index].post) ++bucket_index;
        if (bucket_index == count || post > bounds[bucket_index].post) fail("prefix node interval crosses bucket boundaries");
        if (pre == bounds[bucket_index].pre && (depth != 1 || post != bounds[bucket_index].post))
            fail("prefix bucket root differs from its frozen bounds/depth");
        if (depth == 1 && pre != bounds[bucket_index].pre) fail("prefix depth-one node is not its bucket root");
        if (pre != post) {
            stack.push_back({post, depth});
            peak = std::max(peak, static_cast<uint64_t>(stack.size()));
        }
    }
    if (input.byte() != -1) fail("trailing input after prefix declared node count");
    std::cout << "{\"schema\":\"hot-l1-native-prefix-v1\",\"complete\":true,\"prefix_closed\":true,\"nodes_read\":"
              << nodes << ",\"peak_stack\":" << peak << "}\n";
    if (!std::cout) fail("prefix output write failed");
}

int main(int argc, char** argv) {
    try {
        if (argc == 1) run();
        else if (argc == 2 && std::string(argv[1]) == "--prefix-audit") prefix_audit();
        else fail("unsupported native arguments");
        return 0;
    }
    catch (const std::exception& error) { std::cerr << "hot-l1-native: " << error.what() << '\n'; return 1; }
}
