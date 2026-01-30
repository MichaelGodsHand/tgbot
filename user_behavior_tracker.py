"""
User Behavior Tracking and Personalization System
Tracks user interactions and learns from behavior to improve agent responses
"""

import json
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
from collections import defaultdict
from dataclasses import dataclass, asdict
from contextlib import nullcontext

# Opik integration - use native Opik
try:
    from opik.context_manager import start_as_current_span
    from opik import start_as_current_trace
    OPIK_AVAILABLE = True
except ImportError:
    OPIK_AVAILABLE = False
    def start_as_current_trace(*args, **kwargs):
        return nullcontext()
    def start_as_current_span(*args, **kwargs):
        return nullcontext()

try:
    from supabase import create_client, Client
    import os
    from dotenv import load_dotenv
    load_dotenv()
    SUPABASE_URL = os.getenv("SUPABASE_URL")
    SUPABASE_KEY = os.getenv("SUPABASE_KEY")
    if SUPABASE_URL and SUPABASE_KEY:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    else:
        supabase = None
except:
    supabase = None


@dataclass
class UserInteraction:
    """Record of a user interaction"""
    user_id: str
    interaction_type: str  # "telegram_message", "email_response", "claim_submission"
    agent_name: str
    input_text: str
    output_text: str
    timestamp: str
    satisfaction_score: Optional[float] = None
    feedback: Optional[str] = None
    metadata: Dict[str, Any] = None
    
    def to_dict(self):
        return asdict(self)


@dataclass
class UserProfile:
    """User behavior profile"""
    user_id: str
    interaction_count: int = 0
    preferred_response_style: str = "balanced"  # "concise", "detailed", "balanced"
    common_questions: List[str] = None
    satisfaction_history: List[float] = None
    last_interaction: Optional[str] = None
    preferences: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.common_questions is None:
            self.common_questions = []
        if self.satisfaction_history is None:
            self.satisfaction_history = []
        if self.preferences is None:
            self.preferences = {}


class UserBehaviorTracker:
    """Track and learn from user behavior"""
    
    def __init__(self):
        self.interactions: List[UserInteraction] = []
        self.user_profiles: Dict[str, UserProfile] = {}
        self.off_track_patterns: Dict[str, int] = defaultdict(int)
        self._supabase_tables_checked = False
        self._has_user_interactions_table = False
        self._has_user_learning_table = False
    
    def record_interaction(
        self,
        user_id: str,
        interaction_type: str,
        agent_name: str,
        input_text: str,
        output_text: str,
        satisfaction_score: Optional[float] = None,
        feedback: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ):
        """Record a user interaction"""
        trace_context = start_as_current_trace(
            name="user_behavior.record_interaction",
            input={
                "user_id": user_id,
                "agent": agent_name,
                "interaction_type": interaction_type
            }
        )
        
        with trace_context:
            interaction = UserInteraction(
                user_id=user_id,
                interaction_type=interaction_type,
                agent_name=agent_name,
                input_text=input_text,
                output_text=output_text,
                timestamp=datetime.now().isoformat(),
                satisfaction_score=satisfaction_score,
                feedback=feedback,
                metadata=metadata or {}
            )
            
            self.interactions.append(interaction)
            
            # Update user profile
            if user_id not in self.user_profiles:
                self.user_profiles[user_id] = UserProfile(user_id=user_id)
            
            profile = self.user_profiles[user_id]
            profile.interaction_count += 1
            profile.last_interaction = interaction.timestamp
            
            if satisfaction_score is not None:
                profile.satisfaction_history.append(satisfaction_score)
            
            # Detect off-track behavior
            self._detect_off_track(user_id, interaction)
            
            # Save to Supabase if available (gracefully handle missing tables)
            if supabase:
                # Check table existence on first use
                if not self._supabase_tables_checked:
                    self._check_supabase_tables()
                
                if self._has_user_interactions_table:
                    try:
                        supabase.table('user_interactions').insert(interaction.to_dict()).execute()
                    except Exception as e:
                        print(f"⚠️ Failed to save interaction to Supabase: {e}")
                # If table doesn't exist, data is still stored in memory (self.interactions)
    
    def _detect_off_track(self, user_id: str, interaction: UserInteraction):
        """Detect if user is going off-track"""
        span_context = start_as_current_span(name="detect_off_track")
        with span_context:
            # Patterns that indicate user is off-track
            off_track_indicators = [
                "I don't understand",
                "That's not what I asked",
                "Wrong",
                "No, that's not right",
                "You're not helping",
                "This is useless"
            ]
            
            input_lower = interaction.input_text.lower()
            feedback_lower = (interaction.feedback or "").lower()
            
            # Check for off-track indicators
            for indicator in off_track_indicators:
                if indicator.lower() in input_lower or indicator.lower() in feedback_lower:
                    self.off_track_patterns[user_id] += 1
                    print(f"[BEHAVIOR] ⚠️ User {user_id} appears off-track (pattern: {indicator})")
                    return True
            
            # Low satisfaction score
            if interaction.satisfaction_score is not None and interaction.satisfaction_score < 3:
                self.off_track_patterns[user_id] += 1
                print(f"[BEHAVIOR] ⚠️ User {user_id} low satisfaction: {interaction.satisfaction_score}")
                return True
            
            return False
    
    def _check_supabase_tables(self):
        """Check which Supabase tables are available"""
        if not supabase:
            self._supabase_tables_checked = True
            return
        
        try:
            # Try to query user_interactions table
            supabase.table('user_interactions').select('id').limit(1).execute()
            self._has_user_interactions_table = True
        except Exception as e:
            error_msg = str(e)
            if "Could not find the table" in error_msg or "PGRST205" in error_msg:
                self._has_user_interactions_table = False
            else:
                # Other error, assume table exists
                self._has_user_interactions_table = True
        
        try:
            # Try to query user_learning table
            supabase.table('user_learning').select('id').limit(1).execute()
            self._has_user_learning_table = True
        except Exception as e:
            error_msg = str(e)
            if "Could not find the table" in error_msg or "PGRST205" in error_msg:
                self._has_user_learning_table = False
            else:
                # Other error, assume table exists
                self._has_user_learning_table = True
        
        self._supabase_tables_checked = True
        
        # Log status
        if not self._has_user_interactions_table or not self._has_user_learning_table:
            missing = []
            if not self._has_user_interactions_table:
                missing.append("user_interactions")
            if not self._has_user_learning_table:
                missing.append("user_learning")
            print(f"[BEHAVIOR] ℹ️ Supabase tables not found: {', '.join(missing)}. Data will be stored in memory only.")
            print(f"[BEHAVIOR] ℹ️ To enable Supabase storage, create these tables in your Supabase project.")
    
    def get_user_profile(self, user_id: str) -> UserProfile:
        """Get user behavior profile"""
        if user_id not in self.user_profiles:
            self.user_profiles[user_id] = UserProfile(user_id=user_id)
        return self.user_profiles[user_id]
    
    def analyze_response_style_preference(self, user_id: str) -> str:
        """Analyze user's preferred response style"""
        profile = self.get_user_profile(user_id)
        
        if profile.interaction_count < 3:
            return "balanced"  # Default
        
        # Analyze interaction patterns
        recent_interactions = [
            i for i in self.interactions 
            if i.user_id == user_id 
            and (datetime.now() - datetime.fromisoformat(i.timestamp)) < timedelta(days=7)
        ]
        
        # Check for patterns indicating preference
        concise_indicators = ["short", "brief", "quick", "summary"]
        detailed_indicators = ["more", "details", "explain", "elaborate"]
        
        concise_count = sum(1 for i in recent_interactions if any(ind in i.input_text.lower() for ind in concise_indicators))
        detailed_count = sum(1 for i in recent_interactions if any(ind in i.input_text.lower() for ind in detailed_indicators))
        
        if concise_count > detailed_count:
            return "concise"
        elif detailed_count > concise_count:
            return "detailed"
        else:
            return "balanced"
    
    def get_personalized_prompt_adjustments(self, user_id: str, base_prompt: str) -> str:
        """Get personalized prompt adjustments based on user behavior"""
        span_context = start_as_current_span(name="personalize_prompt")
        with span_context:
            profile = self.get_user_profile(user_id)
            response_style = self.analyze_response_style_preference(user_id)
            
            # Check if user is off-track
            is_off_track = self.off_track_patterns.get(user_id, 0) > 2
            
            adjustments = []
            
            # Response style adjustment
            if response_style == "concise":
                adjustments.append("Be concise and to the point. Avoid unnecessary details.")
            elif response_style == "detailed":
                adjustments.append("Provide detailed explanations and context.")
            
            # Off-track handling
            if is_off_track:
                adjustments.append("The user seems confused or off-track. Be extra clear, ask clarifying questions if needed, and ensure you're addressing their actual question.")
            
            # Low satisfaction handling
            if profile.satisfaction_history:
                avg_satisfaction = sum(profile.satisfaction_history[-5:]) / len(profile.satisfaction_history[-5:])
                if avg_satisfaction < 4:
                    adjustments.append("Previous interactions had low satisfaction. Be more helpful, accurate, and empathetic.")
            
            if adjustments:
                personalized_note = "\n\nPERSONALIZATION NOTES:\n" + "\n".join(f"- {adj}" for adj in adjustments)
                return base_prompt + personalized_note
            
            return base_prompt
    
    def learn_from_feedback(self, user_id: str, feedback: str, satisfaction_score: float):
        """Learn from user feedback to improve future responses"""
        trace_context = start_as_current_trace(
            name="user_behavior.learn_from_feedback",
            input={
                "user_id": user_id,
                "satisfaction_score": satisfaction_score
            }
        )
        
        with trace_context:
            profile = self.get_user_profile(user_id)
            
            # Analyze feedback for improvement signals
            feedback_lower = feedback.lower()
            
            # Extract improvement signals
            improvement_signals = {
                "too_long": "too long" in feedback_lower or "verbose" in feedback_lower,
                "too_short": "too short" in feedback_lower or "brief" in feedback_lower,
                "not_helpful": "not helpful" in feedback_lower or "useless" in feedback_lower,
                "inaccurate": "wrong" in feedback_lower or "incorrect" in feedback_lower,
                "tone_issue": "rude" in feedback_lower or "inappropriate" in feedback_lower
            }
            
            # Update preferences based on feedback
            if improvement_signals["too_long"]:
                profile.preferences["response_length"] = "shorter"
            elif improvement_signals["too_short"]:
                profile.preferences["response_length"] = "longer"
            
            if improvement_signals["tone_issue"]:
                profile.preferences["tone"] = "more_professional"
            
            # Store learning (gracefully handle missing tables)
            if supabase:
                # Check table existence on first use
                if not self._supabase_tables_checked:
                    self._check_supabase_tables()
                
                if self._has_user_learning_table:
                    try:
                        supabase.table('user_learning').insert({
                            "user_id": user_id,
                            "feedback": feedback,
                            "satisfaction_score": satisfaction_score,
                            "improvement_signals": improvement_signals,
                            "timestamp": datetime.now().isoformat()
                        }).execute()
                    except Exception as e:
                        print(f"⚠️ Failed to save learning to Supabase: {e}")
                # If table doesn't exist, learning is still stored in user profile preferences
            
            print(f"[BEHAVIOR] ✅ Learned from feedback for user {user_id}: {improvement_signals}")
    
    def get_reliability_metrics(self) -> Dict[str, Any]:
        """Get system reliability metrics"""
        span_context = start_as_current_span(name="reliability_metrics")
        with span_context:
            total_interactions = len(self.interactions)
            if total_interactions == 0:
                return {"error": "No interactions recorded"}
            
            # Calculate metrics
            avg_satisfaction = sum(
                i.satisfaction_score for i in self.interactions 
                if i.satisfaction_score is not None
            ) / len([i for i in self.interactions if i.satisfaction_score is not None]) if self.interactions else 0
            
            off_track_rate = len(self.off_track_patterns) / len(self.user_profiles) if self.user_profiles else 0
            
            # Agent performance
            agent_performance = defaultdict(lambda: {"count": 0, "avg_satisfaction": 0, "satisfaction_scores": []})
            for interaction in self.interactions:
                agent_perf = agent_performance[interaction.agent_name]
                agent_perf["count"] += 1
                if interaction.satisfaction_score is not None:
                    agent_perf["satisfaction_scores"].append(interaction.satisfaction_score)
            
            for agent_name, perf in agent_performance.items():
                if perf["satisfaction_scores"]:
                    perf["avg_satisfaction"] = sum(perf["satisfaction_scores"]) / len(perf["satisfaction_scores"])
            
            return {
                "total_interactions": total_interactions,
                "unique_users": len(self.user_profiles),
                "average_satisfaction": avg_satisfaction,
                "off_track_rate": off_track_rate,
                "agent_performance": dict(agent_performance),
                "timestamp": datetime.now().isoformat()
            }


# Global tracker instance
behavior_tracker = UserBehaviorTracker()
