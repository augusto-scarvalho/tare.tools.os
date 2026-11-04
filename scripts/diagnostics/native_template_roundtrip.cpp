// Real Jinja -> auto-detection -> grammar/parser -> mapper, without inference.
#include "chat-auto-parser.h"
#include "chat-auto-parser-helpers.h"
#include "chat.h"
#include <nlohmann/json.hpp>
#include <chrono>
#include <fstream>
#include <iostream>
#include <sstream>

int main(int argc, char ** argv) {
    if (argc != 2) {
        std::cerr << "usage: native-template-roundtrip TEMPLATE.jinja\n";
        return 2;
    }
    std::ifstream file(argv[1]);
    if (!file) return 2;
    std::stringstream buffer;
    buffer << file.rdbuf();
    auto started = std::chrono::steady_clock::now();
    common_chat_template tmpl(buffer.str(), "", "<|im_end|>");
    autoparser::autoparser analysis;
    analysis.analyze_template(tmpl);
    json rows = json::array();
    for (const auto & kind : {"string", "nullable", "anyof"}) {
        json schema = {{"type", "string"}};
        if (std::string(kind) == "nullable") schema = {{"type", {"string", "null"}}};
        if (std::string(kind) == "anyof") schema = {{"anyOf", json::array({{{"type", "string"}}, {{"type", "null"}}})}};
        autoparser::generation_params input;
        input.messages = json::array({{{"role", "user"}, {"content", "Probe argument types."}}});
        input.tools = json::array({{{"type", "function"}, {"function", {{"name", "probe"}, {"parameters", {
            {"type", "object"}, {"properties", {{"value", schema}}}, {"required", {"value"}}
        }}}}}});
        input.tool_choice = COMMON_CHAT_TOOL_CHOICE_REQUIRED;
        input.parallel_tool_calls = false;
        input.add_generation_prompt = true;
        input.extra_context = {{"reasoning_effort", "low"}};
        auto generated = autoparser::peg_generator::generate_parser(tmpl, input, analysis);
        if (generated.grammar.empty()) throw std::runtime_error("tool grammar missing");
        common_chat_parser_params parser(generated);
        parser.reasoning_format = COMMON_REASONING_FORMAT_AUTO;
        parser.parser = analysis.build_parser(input, generated.generation_prompt);
        for (const auto & value : json::array({nullptr, "null", "", std::string(64, 'a'), "{\"x\":1}",
                                               "olá\nline 2", "\"quoted\" \\ backslash", "</parameter></function>"})) {
            if (std::string(kind) == "string" && value.is_null()) continue;
            auto history = input;
            history.add_generation_prompt = false;
            history.messages.push_back({{"role", "assistant"}, {"content", ""}, {"reasoning_content", "checked"},
                {"tool_calls", json::array({{{"id", "probe-1"}, {"type", "function"},
                    {"function", {{"name", "probe"}, {"arguments", {{"value", value}}}}}}})}});
            history.messages.push_back({{"role", "tool"}, {"tool_call_id", "probe-1"}, {"content", "done"}});
            std::string rendered = common_chat_template_direct_apply(tmpl, history);
            if (rendered.compare(0, generated.prompt.size(), generated.prompt) != 0) {
                throw std::runtime_error("replay does not share the generation prefix");
            }
            std::string wire = rendered.substr(generated.prompt.size());
            wire = wire.substr(0, wire.find("<|im_end|>"));
            auto msg = common_chat_parse(wire, false, parser);
            json actual;
            bool valid = msg.tool_calls.size() == 1 && msg.tool_calls[0].name == "probe";
            if (valid) {
                auto args = json::parse(msg.tool_calls[0].arguments, nullptr, false);
                valid = !args.is_discarded() && args.contains("value");
                if (valid) actual = args["value"];
            }
            bool prefix_final_equal = false;
            for (size_t n = 0; n <= wire.size(); ++n) {
                auto partial = common_chat_parse(wire.substr(0, n), true, parser);
                if (n == wire.size()) prefix_final_equal = partial == msg;
            }
            rows.push_back({{"schema", kind}, {"expected", value}, {"actual", actual},
                {"reasoning_actual", msg.reasoning_content},
                {"preserved", valid && actual == value}, {"reasoning_preserved", trim_whitespace(msg.reasoning_content) == "checked"},
                {"prefix_final_equal", prefix_final_equal}, {"prefixes_exercised", wire.size() + 1}});
        }
    }
    size_t failures = 0;
    for (const auto & row : rows) {
        if (!row["preserved"].get<bool>() || !row["reasoning_preserved"].get<bool>() ||
            !row["prefix_final_equal"].get<bool>()) ++failures;
    }
    std::ostringstream format;
    format << analysis.tools.format.mode;
    std::cout << json({{"format", format.str()}, {"cases", rows}, {"failures", failures},
        {"wall_ms", std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - started).count()},
        {"inference_calls", 0}, {"scope", "Real template rendering, auto-detection, grammar generation and complete/prefix replay; no sampling or network"}}).dump(2) << '\n';
    return failures ? 1 : 0;
}
