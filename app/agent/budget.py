from dataclasses import dataclass

PRICING = {
    "bedrock-primary": {"input_per_1k": 0.003, "output_per_1k": 0.015},
    "bedrock-fallback": {"input_per_1k": 0.0008, "output_per_1k": 0.004},
}


@dataclass
class BudgetTracker:
    cap_usd: float
    cost_used_usd: float = 0.0

    def cost_of(self, tokens_in: int, tokens_out: int, provider_name: str) -> float:
        pricing = PRICING[provider_name]
        return (tokens_in / 1000) * pricing["input_per_1k"] + (tokens_out / 1000) * pricing["output_per_1k"]

    def would_exceed(self, estimated_cost: float) -> bool:
        return (self.cost_used_usd + estimated_cost) > self.cap_usd

    def record(self, cost: float) -> None:
        self.cost_used_usd += cost
