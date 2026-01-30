import asyncio
import os
import re
import sys
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Dict, Optional, Set
from dotenv import load_dotenv

# Load environment variables FIRST before importing Opik
load_dotenv()

from supabase import create_client, Client
from telegram import Bot, Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    ContextTypes,
    filters,
)
from openai import AsyncOpenAI

# Opik integration - use native Opik directly (self-contained for group folder)
try:
    from opik import configure
    from opik.evaluation import evaluate, evaluate_prompt
    
    # Configure Opik (reads from environment - already loaded above)
    OPIK_API_KEY = os.getenv("OPIK_API_KEY", "")
    OPIK_WORKSPACE = os.getenv("OPIK_WORKSPACE", "impakt")
    OPIK_PROJECT = os.getenv("OPIK_PROJECT", "disaster-monitoring")  # Use project from env
    
    # Set project name in environment BEFORE configuring Opik
    os.environ["OPIK_PROJECT_NAME"] = OPIK_PROJECT
    
    print(f"[OPIK CONFIG] Workspace: {OPIK_WORKSPACE}, Project: {OPIK_PROJECT}")
    
    if OPIK_API_KEY:
        configure(api_key=OPIK_API_KEY, workspace=OPIK_WORKSPACE)
        print(f"[OPIK CONFIG] Configured with API key")
    else:
        configure(workspace=OPIK_WORKSPACE)
        print(f"[OPIK CONFIG] Configured without API key")
    
    # LLM-as-Judge evaluator
    class LLMJudgeEvaluator:
        def __init__(self):
            api_key = os.getenv("OPENAI_API_KEY", "")
            if not api_key:
                raise ValueError("OPENAI_API_KEY environment variable is not set")
            self.client = AsyncOpenAI(api_key=api_key)
        
        async def evaluate_telegram_response(self, user_message: str, bot_response: str, disaster_context: dict):
            """Evaluate Telegram bot response quality using LLM-as-Judge"""
            import json
            prompt = f"""You are an expert evaluator assessing the quality of a disaster response assistant's reply.

USER MESSAGE: {user_message}
BOT RESPONSE: {bot_response}
DISASTER CONTEXT: {json.dumps(disaster_context, indent=2)}

Evaluate the bot's response on the following criteria (rate each 1-10):
1. **Relevance**: Does the response directly address the user's question?
2. **Accuracy**: Is the information factually correct based on the disaster context?
3. **Helpfulness**: Does the response provide useful, actionable information?
4. **Tone**: Is the tone appropriate (empathetic, professional, supportive)?
5. **Completeness**: Does the response fully answer the question or provide sufficient information?

Provide:
- An overall_score (1-10) that represents the overall quality
- Individual scores for each criterion
- A brief reasoning explaining your evaluation

Return JSON in this exact format:
{{
    "overall_score": 8.5,
    "relevance": 9,
    "accuracy": 8,
    "helpfulness": 8,
    "tone": 9,
    "completeness": 7,
    "reasoning": "Brief explanation of the evaluation"
}}"""
            
            response = await self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                response_format={"type": "json_object"}
            )
            return json.loads(response.choices[0].message.content)
    
    llm_judge = LLMJudgeEvaluator()
    OPIK_AVAILABLE = True
    
except ImportError as e:
    # Opik not available - graceful fallback
    print(f"[OPIK] Import failed: {e}")
    print(f"[OPIK] Make sure Opik is installed: pip install opik opik-optimizer")
    llm_judge = None
    OPIK_AVAILABLE = False
    OPIK_PROJECT = os.getenv("OPIK_PROJECT", "disaster-monitoring")
    OPIK_WORKSPACE = os.getenv("OPIK_WORKSPACE", "impakt")

# Import full-featured behavior tracker
try:
    from user_behavior_tracker import UserBehaviorTracker
    behavior_tracker = UserBehaviorTracker()
    print("[BEHAVIOR] ✅ Using full UserBehaviorTracker with personalization and learning")
except ImportError as e:
    print(f"[BEHAVIOR] ⚠️ Failed to import UserBehaviorTracker: {e}")
    print("[BEHAVIOR] Falling back to simple tracker")
    # Fallback simple tracker
    class SimpleBehaviorTracker:
        def __init__(self):
            self.interactions = []
            self.user_profiles = {}
        
        def record_interaction(self, user_id, interaction_type, agent_name, input_text, output_text, satisfaction_score=None, metadata=None):
            self.interactions.append({
                "user_id": user_id,
                "timestamp": datetime.now().isoformat(),
                "satisfaction_score": satisfaction_score
            })
        
        def get_personalized_prompt_adjustments(self, user_id, base_prompt):
            return base_prompt
        
        def get_reliability_metrics(self):
            if not self.interactions:
                return {"total_interactions": 0, "unique_users": 0}
            return {
                "total_interactions": len(self.interactions),
                "unique_users": len(set(i["user_id"] for i in self.interactions)),
                "average_satisfaction": sum(i.get("satisfaction_score", 0) for i in self.interactions) / len(self.interactions) if self.interactions else 0
            }
        
        def learn_from_feedback(self, user_id, feedback, satisfaction_score):
            pass  # No-op for simple tracker
    
    behavior_tracker = SimpleBehaviorTracker()

# Fix Windows console encoding for emojis
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except:
        pass

# Configuration (load_dotenv() already called at top of file)
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPERGROUP_CHAT_ID = -1003723496212
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
# Try common Supabase key environment variable names
SUPABASE_KEY = os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# Polling interval in seconds
POLL_INTERVAL = 10

# Health server port (for Render.com / external pings). Set PORT in env on Render.
HEALTH_PORT = int(os.getenv("PORT", "8080"))


class HealthHandler(BaseHTTPRequestHandler):
    """Serves HEAD/GET / and /health with 200 OK for Render.com health checks."""

    def _send_health(self, body: bool = True):
        if self.path in ("/", "/health"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if body:
                self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_response(404)
            self.end_headers()

    def do_HEAD(self):
        self._send_health(body=False)

    def do_GET(self):
        self._send_health(body=True)

    def log_message(self, format, *args):
        pass  # Suppress default request logging


def run_health_server():
    """Run a minimal HTTP server in a thread for health checks."""
    server = HTTPServer(("0.0.0.0", HEALTH_PORT), HealthHandler)
    server.serve_forever()

# Track processed disasters to avoid duplicates
processed_disasters: Set[str] = set()

# Store disaster data and topic IDs
disaster_topics: Dict[str, Dict] = {}

# Flag to track if we've initialized (marked existing disasters as processed)
_initialized = False


class DisasterAgent:
    """GPT-4o powered agent for providing disaster information"""
    
    def __init__(self, disaster_data: Dict, supabase: Client = None):
        self.disaster_data = disaster_data
        self.supabase = supabase
        self.client = AsyncOpenAI(api_key=OPENAI_API_KEY)
        self.conversation_history: Dict[int, list] = {}  # topic_id -> messages
        self.evaluation_results: list = []  # Store evaluation results for tracking
        
    def _format_disaster_info(self) -> str:
        """Format disaster information from database for the agent"""
        info = f"""Disaster Information:
Title: {self.disaster_data.get('title', 'N/A')}
Location: {self.disaster_data.get('location', 'N/A')}
Occurred At: {self.disaster_data.get('occurred_at', 'N/A')}
Description: {self.disaster_data.get('description', 'No description available')}

"""
        
        if self.disaster_data.get('target_amount'):
            info += f"Target Amount: ${self.disaster_data.get('target_amount'):,.2f}\n"
        
        if self.disaster_data.get('total_donations'):
            info += f"Total Donations: ${self.disaster_data.get('total_donations'):,.2f}\n"
        
        if self.disaster_data.get('vault_address'):
            info += f"Vault Address: {self.disaster_data.get('vault_address')}\n"
        
        if self.disaster_data.get('read_more_link'):
            info += f"Read More: {self.disaster_data.get('read_more_link')}\n"
        
        if self.disaster_data.get('tg_group_link'):
            info += f"Telegram Group: {self.disaster_data.get('tg_group_link')}\n"
        
        return info
    
    def _get_system_prompt(self) -> str:
        """Generate system prompt for the agent"""
        disaster_info = self._format_disaster_info()
        
        return f"""You are a helpful disaster response assistant providing information about a specific disaster event.

{disaster_info}

Your role is to:
1. Answer questions about this disaster event accurately and helpfully
2. Provide information about the location, impact, and current status
3. Share details about donation opportunities and relief efforts
4. Be empathetic and supportive while providing factual information
5. If asked about information not in the database, politely indicate that you only have the information provided above

Always be clear, concise, and helpful. If you don't know something specific, say so rather than making up information."""
    
    async def _fetch_updated_info_if_needed(self, user_message: str) -> Dict:
        """Fetch updated disaster info from DB if user asks for current info"""
        if not self.supabase:
            return self.disaster_data
        
        # Check if user is asking for updated/current information
        update_keywords = ['update', 'latest', 'current', 'now', 'recent', 'new', 'changed', 'donation', 'amount']
        user_lower = user_message.lower()
        
        if any(keyword in user_lower for keyword in update_keywords):
            try:
                disaster_id = str(self.disaster_data['id'])
                def _query():
                    return self.supabase.table('disaster_events').select('*').eq('id', disaster_id).execute()
                
                response = await asyncio.to_thread(_query)
                if response.data:
                    self.disaster_data = response.data[0]
                    return self.disaster_data
            except Exception as e:
                print(f"Warning: Could not fetch updated disaster info: {e}")
        
        return self.disaster_data
    
    async def get_response(self, user_message: str, topic_id: int, user_id: Optional[str] = None) -> str:
        """Get response from GPT-4o agent with Opik tracking"""
        user_id = user_id or f"telegram_{topic_id}"
        
        # Create Opik trace for this interaction
        trace_context = None
        if OPIK_AVAILABLE:
            try:
                from opik import start_as_current_trace
                
                print(f"[OPIK] Creating trace for topic {topic_id}, project: {OPIK_PROJECT}, workspace: {OPIK_WORKSPACE}")
                
                trace_context = start_as_current_trace(
                    name=f"telegram_bot_response_{topic_id}",
                    project_name=OPIK_PROJECT,
                    input={
                        "user_message": user_message,
                        "topic_id": topic_id,
                        "user_id": user_id,
                        "disaster_id": self.disaster_data.get('id') if hasattr(self, 'disaster_data') else None
                    },
                    tags=[OPIK_PROJECT, "telegram_bot", f"topic_{topic_id}"],
                    metadata={
                        "project": OPIK_PROJECT,
                        "workspace": OPIK_WORKSPACE,
                        "agent": "telegram_bot",
                        "topic_id": topic_id,
                        "user_id": user_id
                    },
                    flush=True  # Ensure trace is flushed immediately
                )
                print(f"[OPIK] Trace context created successfully (project: {OPIK_PROJECT}, workspace: {OPIK_WORKSPACE})")
            except Exception as e:
                print(f"[OPIK] Failed to create trace: {e}")
                import traceback
                traceback.print_exc()
                trace_context = None
        else:
            print(f"[OPIK] Opik not available")
            trace_context = None
        
        # Use trace context manager if available
        if trace_context:
            trace_ctx = trace_context
            print(f"[OPIK] Using trace context manager")
        else:
            from contextlib import nullcontext
            trace_ctx = nullcontext()
            print(f"[OPIK] Using null context (no tracing)")
        
        try:
            with trace_ctx:
                # Fetch updated info if needed
                self.disaster_data = await self._fetch_updated_info_if_needed(user_message)
                
                # Get personalized prompt adjustments
                base_prompt = self._get_system_prompt()
                if behavior_tracker:
                    personalized_prompt = behavior_tracker.get_personalized_prompt_adjustments(user_id, base_prompt)
                else:
                    personalized_prompt = base_prompt
                
                # Initialize conversation history for this topic if needed
                if topic_id not in self.conversation_history:
                    self.conversation_history[topic_id] = [
                        {"role": "system", "content": personalized_prompt}
                    ]
                else:
                    # Update system prompt with personalization
                    self.conversation_history[topic_id][0]["content"] = personalized_prompt
                
                # Add user message
                self.conversation_history[topic_id].append({
                    "role": "user",
                    "content": user_message
                })
                
                # Create span for LLM call within the trace
                if OPIK_AVAILABLE:
                    try:
                        from opik.context_manager import start_as_current_span
                        span = start_as_current_span(
                            name="telegram_bot_llm_call",
                            metadata={
                                "input": {
                                    "user_message": user_message[:100],
                                    "model": "gpt-4o-mini",
                                    "topic_id": topic_id
                                }
                            }
                        )
                        span_context = span
                    except:
                        from contextlib import nullcontext
                        span_context = nullcontext()
                else:
                    from contextlib import nullcontext
                    span_context = nullcontext()
                
                with span_context:
                    response = await self.client.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=self.conversation_history[topic_id],
                        temperature=0.3,
                        max_tokens=100
                    )
                    
                    # Update span with output
                    if OPIK_AVAILABLE:
                        try:
                            from opik.opik_context import update_current_span
                            update_current_span(metadata={
                                "output": {
                                    "response_length": len(response.choices[0].message.content) if response.choices else 0,
                                    "model": "gpt-4o-mini"
                                }
                            })
                        except:
                            pass
                
                assistant_message = response.choices[0].message.content
                
                # Add assistant response to history
                self.conversation_history[topic_id].append({
                    "role": "assistant",
                    "content": assistant_message
                })
                
                # Update trace with output
                if OPIK_AVAILABLE and trace_context:
                    try:
                        from opik.opik_context import update_current_trace
                        update_current_trace(
                            output={
                                "bot_response": assistant_message,
                                "response_length": len(assistant_message)
                            },
                            metadata={
                                "project": OPIK_PROJECT,
                                "workspace": OPIK_WORKSPACE,
                                "tags": [OPIK_PROJECT, "telegram_bot"]
                            }
                        )
                        print(f"[OPIK] Updated trace with output (project: {OPIK_PROJECT}, workspace: {OPIK_WORKSPACE})")
                    except Exception as e:
                        print(f"[OPIK] Failed to update trace output: {e}")
                        import traceback
                        traceback.print_exc()
                
                # Variable to store satisfaction score from LLM-as-judge
                satisfaction_score = None
                
                # Evaluate response quality with LLM-as-Judge
                if OPIK_AVAILABLE and llm_judge:
                    try:
                        evaluation = await llm_judge.evaluate_telegram_response(
                            user_message=user_message,
                            bot_response=assistant_message,
                            disaster_context=self.disaster_data
                        )
                        
                        overall_score = evaluation.get('overall_score', 0)
                        reasoning = evaluation.get('reasoning', '')
                        satisfaction_score = overall_score  # Store for behavior tracking
                        
                        print(f"[OPIK EVALUATION] Telegram Response Score: {overall_score:.2f}/10")
                        print(f"  - Relevance: {evaluation.get('relevance', 'N/A')}/10")
                        print(f"  - Accuracy: {evaluation.get('accuracy', 'N/A')}/10")
                        print(f"  - Helpfulness: {evaluation.get('helpfulness', 'N/A')}/10")
                        print(f"  - Tone: {evaluation.get('tone', 'N/A')}/10")
                        print(f"  - Completeness: {evaluation.get('completeness', 'N/A')}/10")
                        if reasoning:
                            print(f"  - Reasoning: {reasoning}")
                        
                        # Store evaluation results
                        evaluation_record = {
                            "topic_id": topic_id,
                            "user_id": user_id,
                            "user_message": user_message[:100],  # Truncate for storage
                            "bot_response": assistant_message[:200],  # Truncate for storage
                            "evaluation": evaluation,
                            "timestamp": datetime.now().isoformat()
                        }
                        self.evaluation_results.append(evaluation_record)
                        
                        # Learn from LLM-as-judge feedback
                        if behavior_tracker and hasattr(behavior_tracker, 'learn_from_feedback'):
                            try:
                                # Use reasoning as feedback text, normalize score to 0-10 range
                                feedback_text = reasoning or f"LLM-as-judge evaluation: Overall score {overall_score:.2f}/10"
                                behavior_tracker.learn_from_feedback(
                                    user_id=user_id,
                                    feedback=feedback_text,
                                    satisfaction_score=overall_score  # Already 1-10 scale
                                )
                            except Exception as e:
                                print(f"[BEHAVIOR] Failed to learn from feedback: {e}")
                        
                        # Send feedback scores to Opik (multiple scores for detailed evaluation)
                        try:
                            from opik.opik_context import update_current_trace
                            
                            feedback_scores_list = [
                                {
                                    "name": "overall_quality",
                                    "value": float(overall_score),
                                    "reason": evaluation.get('reasoning', 'Overall response quality evaluation')
                                },
                                {
                                    "name": "relevance",
                                    "value": float(evaluation.get('relevance', 0)),
                                    "reason": "How well the response addresses the user's question"
                                },
                                {
                                    "name": "accuracy",
                                    "value": float(evaluation.get('accuracy', 0)),
                                    "reason": "Factual correctness based on disaster context"
                                },
                                {
                                    "name": "helpfulness",
                                    "value": float(evaluation.get('helpfulness', 0)),
                                    "reason": "Usefulness and actionability of the information provided"
                                },
                                {
                                    "name": "tone",
                                    "value": float(evaluation.get('tone', 0)),
                                    "reason": "Appropriateness of tone (empathetic, professional, supportive)"
                                },
                                {
                                    "name": "completeness",
                                    "value": float(evaluation.get('completeness', 0)),
                                    "reason": "Whether the response fully answers the question"
                                }
                            ]
                            
                            update_current_trace(
                                feedback_scores=feedback_scores_list,
                                metadata={
                                    "project": OPIK_PROJECT,
                                    "workspace": OPIK_WORKSPACE,
                                    "tags": [OPIK_PROJECT, "telegram_bot", "llm_judge"],
                                    "evaluation": evaluation
                                }
                            )
                            print(f"[OPIK] Sent {len(feedback_scores_list)} feedback scores to Opik")
                        except Exception as e:
                            print(f"[OPIK] Failed to send feedback scores: {e}")
                            import traceback
                            traceback.print_exc()
                    except Exception as e:
                        print(f"[OPIK EVALUATION] Evaluation failed: {e}")
                        import traceback
                        traceback.print_exc()
                
                # Record interaction for behavior tracking
                # satisfaction_score is set above from LLM-as-judge evaluation if available
                if behavior_tracker:
                    behavior_tracker.record_interaction(
                        user_id=user_id,
                        interaction_type="telegram_message",
                        agent_name="telegram_bot",
                        input_text=user_message,
                        output_text=assistant_message,
                        satisfaction_score=satisfaction_score,
                        metadata={"topic_id": topic_id, "disaster_id": self.disaster_data.get('id')}
                    )
                
                # Keep conversation history manageable (last 20 messages)
                if len(self.conversation_history[topic_id]) > 20:
                    # Keep system prompt and last 19 messages
                    system_msg = self.conversation_history[topic_id][0]
                    recent_messages = self.conversation_history[topic_id][-19:]
                    self.conversation_history[topic_id] = [system_msg] + recent_messages
                
                return assistant_message
                
        except Exception as e:
            error_msg = f"I apologize, but I encountered an error: {str(e)}. Please try again."
            
            # Update trace with error
            if OPIK_AVAILABLE and trace_context:
                try:
                    from opik.opik_context import update_current_trace
                    update_current_trace(
                        output={"error": str(e), "error_message": error_msg},
                        metadata={"project": OPIK_PROJECT, "error": True}
                    )
                    print(f"[OPIK] Updated trace with error")
                except Exception as e2:
                    print(f"[OPIK] Failed to update trace with error: {e2}")
            
            # Record error interaction
            if behavior_tracker:
                behavior_tracker.record_interaction(
                    user_id=user_id,
                    interaction_type="telegram_message",
                    agent_name="telegram_bot",
                    input_text=user_message,
                    output_text=error_msg,
                    satisfaction_score=0,
                    metadata={"error": str(e), "topic_id": topic_id}
                )
            return error_msg
        finally:
            # Trace context manager will automatically flush on exit
            if trace_context:
                print(f"[OPIK] Trace context will be flushed on exit")
    
    def get_evaluation_statistics(self) -> Dict:
        """Get statistics about evaluation results"""
        if not self.evaluation_results:
            return {
                "total_evaluations": 0,
                "average_overall_score": 0,
                "average_relevance": 0,
                "average_accuracy": 0,
                "average_helpfulness": 0,
                "average_tone": 0,
                "average_completeness": 0
            }
        
        total = len(self.evaluation_results)
        evaluations = [e["evaluation"] for e in self.evaluation_results]
        
        return {
            "total_evaluations": total,
            "average_overall_score": sum(e.get("overall_score", 0) for e in evaluations) / total,
            "average_relevance": sum(e.get("relevance", 0) for e in evaluations) / total,
            "average_accuracy": sum(e.get("accuracy", 0) for e in evaluations) / total,
            "average_helpfulness": sum(e.get("helpfulness", 0) for e in evaluations) / total,
            "average_tone": sum(e.get("tone", 0) for e in evaluations) / total,
            "average_completeness": sum(e.get("completeness", 0) for e in evaluations) / total,
            "recent_evaluations": self.evaluation_results[-10:]  # Last 10 evaluations
        }


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming messages in disaster topics"""
    if not update.message or not update.message.text:
        return
    
    # Check if message is in a topic
    message_thread_id = update.message.message_thread_id
    if not message_thread_id:
        return  # Not in a topic, ignore
    
    # Debug logging
    print(f"📨 Received message in topic {message_thread_id}: {update.message.text[:50]}...")
    
    # Ignore bot's own messages
    if update.message.from_user and update.message.from_user.is_bot:
        return
    
    # Check if bot is mentioned/tagged in the message
    # Get bot username (fetch if not cached)
    bot_username = context.bot.username
    if not bot_username:
        try:
            bot_info = await context.bot.get_me()
            bot_username = bot_info.username
        except:
            bot_username = None
    
    message_text = update.message.text or ""
    
    # Check for mentions in message entities
    is_mentioned = False
    if update.message.entities and bot_username:
        for entity in update.message.entities:
            if entity.type == "mention":
                mention_text = message_text[entity.offset:entity.offset + entity.length]
                if mention_text.lower() == f"@{bot_username}".lower():
                    is_mentioned = True
                    break
    
    # Also check if bot username appears in text (case-insensitive)
    if not is_mentioned and bot_username:
        if f"@{bot_username}".lower() in message_text.lower():
            is_mentioned = True
    
    # Only respond if bot is mentioned
    if not is_mentioned:
        return
    
    # Find which disaster this topic belongs to
    disaster_id = None
    agent = None
    
    for did, data in disaster_topics.items():
        if data.get('topic_id') == message_thread_id:
            disaster_id = did
            agent = data.get('agent')
            break
    
    if not agent:
        print(f"⚠️ No agent found for topic {message_thread_id}. Available topics: {list(disaster_topics.keys())}")
        return  # Topic not found or agent not initialized
    
    print(f"✅ Found agent for disaster {disaster_id} in topic {message_thread_id}")
    
    # Get user message and remove the mention
    user_message = message_text
    # Remove @bot_username mentions from the message (case-insensitive)
    if bot_username:
        user_message = re.sub(rf"@{bot_username}\s*", "", user_message, flags=re.IGNORECASE).strip()
    
    # Get user ID for behavior tracking
    user_id = f"telegram_{update.message.from_user.id}" if update.message.from_user else f"telegram_{message_thread_id}"
    
    # Get response from agent
    try:
        # Show typing indicator
        await context.bot.send_chat_action(
            chat_id=SUPERGROUP_CHAT_ID,
            message_thread_id=message_thread_id,
            action='typing'
        )
        
        response = await agent.get_response(user_message, message_thread_id, user_id=user_id)
        
        # Telegram has a 4096 character limit per message
        if len(response) > 4096:
            # Split into chunks
            chunks = [response[i:i+4096] for i in range(0, len(response), 4096)]
            for chunk in chunks:
                await context.bot.send_message(
                    chat_id=SUPERGROUP_CHAT_ID,
                    message_thread_id=message_thread_id,
                    text=chunk
                )
        else:
            # Send response in the topic
            await context.bot.send_message(
                chat_id=SUPERGROUP_CHAT_ID,
                message_thread_id=message_thread_id,
                text=response
            )
    except Exception as e:
        print(f"Error handling message: {e}")
        import traceback
        traceback.print_exc()
        # Try to send error message to user
        try:
            await context.bot.send_message(
                chat_id=SUPERGROUP_CHAT_ID,
                message_thread_id=message_thread_id,
                text="I apologize, but I encountered an error processing your message. Please try again."
            )
        except:
            pass


async def create_disaster_topic_and_agent(
    bot: Bot,
    disaster: Dict,
    supabase: Client
) -> Optional[int]:
    """Create a Telegram topic for a disaster and initialize the agent"""
    disaster_id = str(disaster['id'])
    
    # Skip if already processed
    if disaster_id in processed_disasters:
        return None
    
    try:
        # Create topic with disaster title
        topic_name = f"{disaster.get('title', 'Disaster Event')[:64]}"  # Telegram limit
        topic = await bot.create_forum_topic(
            chat_id=SUPERGROUP_CHAT_ID,
            name=topic_name
        )
        
        topic_id = topic.message_thread_id
        
        # Generate topic URL
        # Telegram forum topic URL format: https://t.me/c/{chat_id}/{topic_id}
        # For supergroups, remove the -100 prefix from chat_id
        chat_id_for_url = str(SUPERGROUP_CHAT_ID).replace('-100', '')
        topic_url = f"https://t.me/c/{chat_id_for_url}/{topic_id}"
        
        # Log the topic URL
        print(f"🔗 Topic URL: {topic_url}")
        
        # Update the disaster record in Supabase with the topic URL
        try:
            def _update_disaster():
                return supabase.table('disaster_events').update({
                    'tg_group_link': topic_url
                }).eq('id', disaster_id).execute()
            
            await asyncio.to_thread(_update_disaster)
            print(f"✅ Updated disaster {disaster_id} with topic URL in database")
        except Exception as update_error:
            print(f"⚠️ Warning: Failed to update topic URL in database: {update_error}")
        
        # Initialize agent with disaster data and supabase client
        agent = DisasterAgent(disaster, supabase)
        
        # Update disaster data with the new topic URL
        disaster['tg_group_link'] = topic_url
        
        # Store disaster info
        disaster_topics[disaster_id] = {
            'topic_id': topic_id,
            'agent': agent,
            'disaster': disaster,
            'created_at': datetime.now()
        }
        
        # Send welcome message with disaster info FIRST (before marking as processed)
        # Use HTML parse mode which is more forgiving than Markdown
        def escape_html(text):
            """Escape HTML special characters"""
            if not text:
                return ""
            text = str(text)
            text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            return text
        
        title = escape_html(disaster.get('title', 'Unknown'))
        location = escape_html(disaster.get('location', 'N/A'))
        occurred_at = escape_html(disaster.get('occurred_at', 'N/A'))
        description = escape_html(disaster.get('description', 'No description available'))
        
        # Truncate description if too long (Telegram has 4096 char limit)
        if len(description) > 2000:
            description = description[:2000] + "..."
        
        welcome_message = f"""🚨 <b>Disaster Alert: {title}</b>

📍 <b>Location:</b> {location}
📅 <b>Occurred:</b> {occurred_at}

{description}

"""
        
        if disaster.get('target_amount'):
            amount = escape_html(f"${disaster.get('target_amount'):,.2f}")
            welcome_message += f"💰 <b>Target Amount:</b> {amount}\n"
        
        if disaster.get('total_donations'):
            donations = escape_html(f"${disaster.get('total_donations'):,.2f}")
            welcome_message += f"💵 <b>Total Donations:</b> {donations}\n"
        
        if disaster.get('vault_address'):
            vault = escape_html(disaster.get('vault_address'))
            welcome_message += f"🔗 <b>Vault Address:</b> <code>{vault}</code>\n"
        
        if disaster.get('read_more_link'):
            read_more = escape_html(disaster.get('read_more_link'))
            welcome_message += f"📖 <b>Read More:</b> {read_more}\n"
        
        # Get bot username for mention instructions
        try:
            bot_info = await bot.get_me()
            bot_username = bot_info.username
            welcome_message += f"\n🤖 I'm here to help answer questions about this disaster. Tag me (@{bot_username}) to ask questions!"
        except:
            welcome_message += "\n🤖 I'm here to help answer questions about this disaster. Tag me to ask questions!"
        
        # Send welcome message with error handling
        try:
            await bot.send_message(
                chat_id=SUPERGROUP_CHAT_ID,
                message_thread_id=topic_id,
                text=welcome_message,
                parse_mode='HTML'
            )
            print(f"✅ Welcome message sent successfully")
        except Exception as msg_error:
            print(f"⚠️ Warning: Failed to send welcome message with HTML: {msg_error}")
            # Fallback: Send plain text message without formatting
            try:
                plain_message = f"""🚨 Disaster Alert: {disaster.get('title', 'Unknown')}

📍 Location: {disaster.get('location', 'N/A')}
📅 Occurred: {disaster.get('occurred_at', 'N/A')}

{disaster.get('description', 'No description available')[:2000]}

"""
                if disaster.get('target_amount'):
                    plain_message += f"💰 Target Amount: ${disaster.get('target_amount'):,.2f}\n"
                if disaster.get('total_donations'):
                    plain_message += f"💵 Total Donations: ${disaster.get('total_donations'):,.2f}\n"
                if disaster.get('vault_address'):
                    plain_message += f"🔗 Vault Address: {disaster.get('vault_address')}\n"
                if disaster.get('read_more_link'):
                    plain_message += f"📖 Read More: {disaster.get('read_more_link')}\n"
                
                plain_message += "\n🤖 I'm here to help answer questions about this disaster. Tag me to ask questions!"
                
                await bot.send_message(
                    chat_id=SUPERGROUP_CHAT_ID,
                    message_thread_id=topic_id,
                    text=plain_message
                )
                print(f"✅ Sent welcome message as plain text")
            except Exception as fallback_error:
                print(f"❌ Failed to send welcome message even with plain text: {fallback_error}")
                import traceback
                traceback.print_exc()
                # Try minimal message as last resort
                try:
                    await bot.send_message(
                        chat_id=SUPERGROUP_CHAT_ID,
                        message_thread_id=topic_id,
                        text=f"🚨 Disaster Alert: {disaster.get('title', 'Unknown')}\n\n🤖 I'm here to help answer questions about this disaster. Tag me to ask questions!"
                    )
                    print(f"✅ Sent minimal welcome message")
                except Exception as minimal_error:
                    print(f"❌ Failed to send even minimal message: {minimal_error}")
        
        # Mark as processed only after welcome message is sent (or attempted)
        processed_disasters.add(disaster_id)
        
        print(f"✅ Created topic for disaster: {disaster.get('title')} (ID: {disaster_id}, Topic ID: {topic_id})")
        print(f"📝 Agent is ready to receive messages in topic {topic_id}")
        
        return topic_id
        
    except Exception as e:
        print(f"❌ Error creating topic for disaster {disaster_id}: {e}")
        return None


async def initialize_existing_disasters(supabase: Client):
    """Mark all existing disasters as processed without creating topics"""
    global _initialized
    if _initialized:
        return
    
    try:
        # Query for all existing disasters
        def _query_all_disasters():
            return supabase.table('disaster_events').select('id').execute()
        
        response = await asyncio.to_thread(_query_all_disasters)
        disasters = response.data if response.data else []
        
        # Mark all existing disasters as processed
        for disaster in disasters:
            disaster_id = str(disaster['id'])
            processed_disasters.add(disaster_id)
        
        print(f"📋 Initialized: Marked {len(disasters)} existing disaster(s) as processed")
        _initialized = True
        
    except Exception as e:
        print(f"❌ Error initializing existing disasters: {e}")
        import traceback
        traceback.print_exc()


async def check_new_disasters(bot: Bot, supabase: Client):
    """Check for new disaster events in Supabase"""
    try:
        # Query for all disasters, ordered by created_at descending
        # Run sync Supabase query in thread pool to avoid blocking
        def _query_disasters():
            return supabase.table('disaster_events').select('*').order('created_at', desc=True).execute()
        
        response = await asyncio.to_thread(_query_disasters)
        disasters = response.data if response.data else []
        
        # Process new disasters
        new_count = 0
        for disaster in disasters:
            disaster_id = str(disaster['id'])
            
            # Skip if already processed
            if disaster_id in processed_disasters:
                continue
            
            # Create topic and agent for new disaster
            topic_id = await create_disaster_topic_and_agent(bot, disaster, supabase)
            if topic_id:
                new_count += 1
        
        if new_count > 0:
            print(f"📊 Processed {new_count} new disaster(s)")
            
    except Exception as e:
        print(f"❌ Error checking for new disasters: {e}")
        import traceback
        traceback.print_exc()


async def update_disaster_info(bot: Bot, supabase: Client):
    """Update disaster information in existing topics"""
    try:
        # Get all disasters
        # Run sync Supabase query in thread pool to avoid blocking
        def _query_all_disasters():
            return supabase.table('disaster_events').select('*').execute()
        
        response = await asyncio.to_thread(_query_all_disasters)
        disasters = response.data if response.data else []
        
        for disaster in disasters:
            disaster_id = str(disaster['id'])
            
            # Only update if topic exists
            if disaster_id in disaster_topics:
                topic_data = disaster_topics[disaster_id]
                topic_id = topic_data['topic_id']
                
                # Update disaster data in agent
                topic_data['disaster'] = disaster
                topic_data['agent'].disaster_data = disaster
                
                # Clear conversation history to refresh with new data
                if topic_id in topic_data['agent'].conversation_history:
                    topic_data['agent'].conversation_history[topic_id] = [
                        {"role": "system", "content": topic_data['agent']._get_system_prompt()}
                    ]
                
    except Exception as e:
        print(f"❌ Error updating disaster info: {e}")


async def monitor_disasters():
    """Main monitoring loop"""
    # Initialize Supabase client
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set in environment variables")
    
    # Create Supabase client
    # Handle version incompatibility issues
    try:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    except (TypeError, AttributeError) as e:
        error_msg = str(e).lower()
        if "proxy" in error_msg or "unexpected keyword argument" in error_msg:
            print("\n" + "="*60)
            print("❌ Supabase client version incompatibility detected!")
            print("="*60)
            print("\n💡 Solution: Please reinstall dependencies:")
            print("\n   pip uninstall supabase httpx gotrue -y")
            print("   pip install supabase httpx")
            print("\n   OR")
            print("\n   pip install --upgrade -r requirements.txt")
            print("\n" + "="*60)
            raise ValueError(
                "Supabase client initialization failed due to dependency version mismatch.\n"
                "Please run: pip install --upgrade supabase httpx"
            )
        raise
    
    # Initialize Telegram bot
    bot = Bot(token=BOT_TOKEN)
    
    # Initialize Telegram application for message handling
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # Add message handler
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Start polling in background
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    
    print("🤖 Disaster monitor started. Listening for new disasters...")
    print(f"📊 Checking every {POLL_INTERVAL} seconds")
    
    # Initialize: Mark all existing disasters as processed (don't create topics for them)
    await initialize_existing_disasters(supabase)
    
    # Initial check for any disasters added between initialization and now
    await check_new_disasters(bot, supabase)
    
    # Periodic monitoring loop
    while True:
        try:
            await asyncio.sleep(POLL_INTERVAL)
            await check_new_disasters(bot, supabase)
            # Update existing disaster info periodically (every 5 checks)
            if len(processed_disasters) % 5 == 0:
                await update_disaster_info(bot, supabase)
        except KeyboardInterrupt:
            print("\n🛑 Stopping monitor...")
            await application.updater.stop()
            await application.stop()
            await application.shutdown()
            break
        except Exception as e:
            print(f"❌ Error in monitoring loop: {e}")
            await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    # Validate environment variables
    missing_vars = []
    
    if not OPENAI_API_KEY:
        missing_vars.append("OPENAI_API_KEY")
    
    if not SUPABASE_URL:
        missing_vars.append("SUPABASE_URL")
    
    if not SUPABASE_KEY:
        missing_vars.append("SUPABASE_KEY (or SUPABASE_ANON_KEY or SUPABASE_SERVICE_ROLE_KEY)")
    
    if missing_vars:
        print("❌ Missing required environment variables:")
        for var in missing_vars:
            print(f"   - {var}")
        print("\nPlease set these in your .env file or environment variables.")
        print("Example .env file:")
        print("  SUPABASE_URL=https://your-project.supabase.co")
        print("  SUPABASE_KEY=your-supabase-anon-key")
        print("  OPENAI_API_KEY=your-openai-api-key")
        exit(1)
    
    # Start health server in background (for Render.com / keep-alive pings)
    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()
    print(f"Health endpoint: http://0.0.0.0:{HEALTH_PORT}/health")

    # Run the monitor
    try:
        asyncio.run(monitor_disasters())
    except KeyboardInterrupt:
        print("\n👋 Monitor stopped by user")
    except Exception as e:
        print(f"\n❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()
