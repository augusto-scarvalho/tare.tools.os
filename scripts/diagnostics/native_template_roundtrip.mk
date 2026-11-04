ifndef SLOP_ROOT
$(error SLOP_ROOT must select the slop.cpp checkout to test)
endif
ifndef BUILD
$(error BUILD must select an owned disposable build directory)
endif
HERE := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
CXXFLAGS := -std=c++17 -O0 -ffunction-sections -fdata-sections -MMD -MP
CPPFLAGS := -I$(SLOP_ROOT)/common -I$(SLOP_ROOT)/include -I$(SLOP_ROOT)/ggml/include -I$(SLOP_ROOT)/vendor
SOURCES := chat-auto-parser-generator chat-peg-parser peg-parser json-schema-to-grammar unicode log common console chat-diff-analyzer chat-auto-parser-helpers trie chat jinja/lexer jinja/parser jinja/runtime jinja/value jinja/string jinja/caps
OBJECTS := $(addprefix $(BUILD)/,$(addsuffix .o,$(SOURCES))) $(BUILD)/roundtrip.o $(BUILD)/ggml.o
.PHONY: all
all: $(BUILD)/native-template-roundtrip
$(BUILD)/native-template-roundtrip: $(OBJECTS)
	$(CXX) $(OBJECTS) -Wl,--gc-sections -pthread -o $@
$(BUILD)/roundtrip.o: $(HERE)/native_template_roundtrip.cpp
	@mkdir -p $(@D)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -c $< -o $@
$(BUILD)/ggml.o: $(SLOP_ROOT)/ggml/src/ggml.c
	@mkdir -p $(@D)
	$(CC) $(CPPFLAGS) -I$(SLOP_ROOT)/ggml/src -D_GNU_SOURCE '-DGGML_VERSION="diagnostic"' '-DGGML_COMMIT="source-selected"' -std=c11 -O0 -ffunction-sections -fdata-sections -MMD -MP -c $< -o $@
$(BUILD)/%.o: $(SLOP_ROOT)/common/%.cpp
	@mkdir -p $(@D)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -c $< -o $@
-include $(OBJECTS:.o=.d)
