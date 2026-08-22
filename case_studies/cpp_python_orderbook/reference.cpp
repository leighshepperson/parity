#include <algorithm>
#include <cstdint>
#include <iostream>
#include <limits>
#include <string>
#include <unordered_map>
#include <vector>

struct Order {
    std::int64_t id;
    std::string instrument;
    char side;
    std::int64_t price_ticks;
    std::int64_t remaining_lots;
    std::int64_t sequence;
    bool active;
};

struct Fill {
    std::int64_t trade_sequence;
    std::string instrument;
    std::int64_t maker_order_id;
    std::int64_t taker_order_id;
    std::int64_t buyer_order_id;
    std::int64_t seller_order_id;
    std::int64_t price_ticks;
    std::int64_t quantity_lots;
    std::int64_t quantity_units;
};

static bool crosses(const Order& resting, char incoming_side, std::int64_t incoming_price) {
    if (incoming_side == 'B') {
        return resting.side == 'S' && resting.price_ticks <= incoming_price;
    }
    return resting.side == 'B' && resting.price_ticks >= incoming_price;
}

static bool better_maker(const Order& candidate, const Order& incumbent, char incoming_side) {
    if (candidate.price_ticks != incumbent.price_ticks) {
        if (incoming_side == 'B') {
            return candidate.price_ticks < incumbent.price_ticks;
        }
        return candidate.price_ticks > incumbent.price_ticks;
    }
    return candidate.sequence < incumbent.sequence;
}

int main() {
    std::string marker;
    std::size_t instrument_count = 0;
    if (!(std::cin >> marker >> instrument_count) || marker != "I") {
        return 3;
    }

    std::unordered_map<std::string, std::int64_t> lot_sizes;
    for (std::size_t index = 0; index < instrument_count; ++index) {
        std::string instrument;
        std::int64_t lot_size = 0;
        if (!(std::cin >> instrument >> lot_size) || lot_size <= 0) {
            return 3;
        }
        lot_sizes[instrument] = lot_size;
    }

    std::size_t event_count = 0;
    if (!(std::cin >> marker >> event_count) || marker != "E") {
        return 3;
    }

    std::vector<Order> book;
    std::unordered_map<std::int64_t, std::size_t> resting_by_id;
    std::vector<Fill> fills;

    for (std::size_t event_index = 0; event_index < event_count; ++event_index) {
        std::int64_t sequence = 0;
        std::string event_type;
        std::int64_t order_id = 0;
        std::string instrument;
        std::string side_token;
        std::int64_t price_ticks = 0;
        std::int64_t quantity_lots = 0;
        if (!(std::cin >> sequence >> event_type >> order_id >> instrument >> side_token
              >> price_ticks >> quantity_lots)) {
            return 3;
        }

        if (event_type == "CANCEL") {
            const auto found = resting_by_id.find(order_id);
            if (found == resting_by_id.end() || !book[found->second].active) {
                std::cout << "ERROR inactive_cancel\n";
                return 0;
            }
            book[found->second].active = false;
            continue;
        }

        if (event_type != "NEW" || side_token.size() != 1 ||
            (side_token[0] != 'B' && side_token[0] != 'S') ||
            lot_sizes.find(instrument) == lot_sizes.end() || quantity_lots <= 0) {
            return 3;
        }

        Order incoming{
            order_id,
            instrument,
            side_token[0],
            price_ticks,
            quantity_lots,
            sequence,
            true,
        };

        while (incoming.remaining_lots > 0) {
            std::size_t best_index = std::numeric_limits<std::size_t>::max();
            for (std::size_t index = 0; index < book.size(); ++index) {
                const Order& resting = book[index];
                if (!resting.active || resting.instrument != incoming.instrument ||
                    !crosses(resting, incoming.side, incoming.price_ticks)) {
                    continue;
                }
                if (best_index == std::numeric_limits<std::size_t>::max() ||
                    better_maker(resting, book[best_index], incoming.side)) {
                    best_index = index;
                }
            }

            if (best_index == std::numeric_limits<std::size_t>::max()) {
                break;
            }

            Order& maker = book[best_index];
            const std::int64_t matched_lots =
                std::min(incoming.remaining_lots, maker.remaining_lots);
            const std::int64_t buyer_id = incoming.side == 'B' ? incoming.id : maker.id;
            const std::int64_t seller_id = incoming.side == 'S' ? incoming.id : maker.id;
            fills.push_back(Fill{
                sequence,
                instrument,
                maker.id,
                incoming.id,
                buyer_id,
                seller_id,
                maker.price_ticks,
                matched_lots,
                matched_lots * lot_sizes.at(instrument),
            });
            incoming.remaining_lots -= matched_lots;
            maker.remaining_lots -= matched_lots;
            if (maker.remaining_lots == 0) {
                maker.active = false;
            }
        }

        if (incoming.remaining_lots > 0) {
            resting_by_id[incoming.id] = book.size();
            book.push_back(incoming);
        }
    }

    std::cout << "OK " << fills.size() << '\n';
    for (const Fill& fill : fills) {
        std::cout << fill.trade_sequence << ' ' << fill.instrument << ' '
                  << fill.maker_order_id << ' ' << fill.taker_order_id << ' '
                  << fill.buyer_order_id << ' ' << fill.seller_order_id << ' '
                  << fill.price_ticks << ' ' << fill.quantity_lots << ' '
                  << fill.quantity_units << '\n';
    }
    return 0;
}
