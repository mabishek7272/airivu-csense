/* ==========================================================================
   spAIder NEO - Main Interactive Script
   Domain: about.spaider.life
   ========================================================================== */

document.addEventListener('DOMContentLoaded', () => {

  // --- 1. Core Feature Tabs Switcher ---
  const tabBtns = document.querySelectorAll('.tab-btn');
  const tabContents = document.querySelectorAll('.tab-content');

  tabBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      const targetTab = btn.getAttribute('data-tab');

      tabBtns.forEach(b => b.classList.remove('active'));
      tabContents.forEach(c => c.classList.remove('active'));

      btn.classList.add('active');
      const activeContent = document.getElementById(`tab-${targetTab}`);
      if (activeContent) {
        activeContent.classList.add('active');
      }
    });
  });

  // --- 2. spAIder Search™ Dossier Navigation Tabs ---
  const dossierTabs = document.querySelectorAll('.dossier-tab-item');
  const dossierPanes = document.querySelectorAll('.dossier-pane');

  dossierTabs.forEach(tab => {
    tab.addEventListener('click', () => {
      const paneId = tab.getAttribute('data-pane');

      dossierTabs.forEach(t => t.classList.remove('active'));
      dossierPanes.forEach(p => p.classList.remove('active'));

      tab.classList.add('active');
      const activePane = document.getElementById(`pane-${paneId}`);
      if (activePane) {
        activePane.classList.add('active');
      }
    });
  });

  // --- 3. Interactive Score Rationale Modal Drawer ---
  const scoreCards = document.querySelectorAll('.score-interactive-card');
  const modalOverlay = document.getElementById('score-modal-overlay');
  const modalTitle = document.getElementById('modal-score-title');
  const modalVal = document.getElementById('modal-score-val');
  const modalRationale = document.getElementById('modal-rationale-text');
  const modalEvidence = document.getElementById('modal-evidence-text');
  const modalConfidence = document.getElementById('modal-confidence-text');
  const modalFactors = document.getElementById('modal-factors-list');
  const modalClose = document.getElementById('modal-close-btn');

  const scoreDetailsData = {
    'business-match': {
      title: 'Business Match Score',
      val: '94%',
      rationale: 'High alignment between target organization’s tech modernization goals and your enterprise product portfolio. Target exhibits strong procurement signals in AI & Edge Computing.',
      evidence: 'Verified via Q2 Public Press Release, TechCrunch interview (May 2026), and recent job postings for Principal AI Architects.',
      confidence: '96% (Verified across 5 public data sources)',
      factors: ['Product-Market Fit: 95%', 'Budget Authority: High', 'Strategic Synergy: 92%']
    },
    'opportunity': {
      title: 'Opportunity Score',
      val: '92%',
      rationale: 'Substantial immediate potential for joint venture development and co-marketing in the APAC & EU enterprise markets.',
      evidence: 'Corporate filing indicates $14M expansion fund allocated for strategic partnerships in FY26.',
      confidence: '94% (Verified via SEC filings & company press room)',
      factors: ['Market Timing: Optimal', 'Expansion Urgency: High', 'Competitor Pressure: Moderate']
    },
    'lead-qual': {
      title: 'Lead Qualification Score',
      val: '88%',
      rationale: 'Decision-maker holds VP-level purchasing clearance with active budget cycle renewing within 30 days.',
      evidence: 'Direct executive endorsement in regional tech symposium & past vendor selection patterns.',
      confidence: '91% (Verified via LinkedIn & organizational graph)',
      factors: ['Authority Level: Tier 1 Executive', 'Need Urgency: Immediate', 'Timeline: 30 Days']
    },
    'partnership': {
      title: 'Partnership Potential',
      val: '95%',
      rationale: 'Complementary product ecosystems with zero overlapping client friction. Outstanding candidate for strategic API integration.',
      evidence: 'Public GitHub open-source contributions & official developer portal roadmap.',
      confidence: '97% (Verified via public developer docs & patent records)',
      factors: ['Ecosystem Fit: Perfect', 'Technical Interoperability: 98%', 'Brand Alignment: 94%']
    },
    'customer-fit': {
      title: 'Customer Fit Score',
      val: '91%',
      rationale: 'Company scale (500+ employees, $80M ARR) matches your ideal customer profile (ICP) benchmark metrics.',
      evidence: 'Dun & Bradstreet business profile & official corporate website analytics.',
      confidence: '95% (Verified via corporate registry & public report)',
      factors: ['Employee Count Alignment: 100%', 'ARR Fit: 92%', 'Tech Stack Compatibility: 90%']
    },
    'influence': {
      title: 'Professional Influence Score',
      val: '96%',
      rationale: 'Key opinion leader with 45K+ industry followers, frequent keynote speeches at global tech summits, and board seats.',
      evidence: 'Keynotes at AI Global Summit 2025/2026, 12 published whitepapers, and 4 granted patents.',
      confidence: '98% (Verified via IEEE citations, Patent office & conference agendas)',
      factors: ['Thought Leadership: Top 1%', 'Network Reach: 45K+', 'Citation Index: High']
    }
  };

  scoreCards.forEach(card => {
    card.addEventListener('click', () => {
      const scoreKey = card.getAttribute('data-score');
      const data = scoreDetailsData[scoreKey];

      if (data) {
        modalTitle.textContent = data.title;
        modalVal.textContent = data.val;
        modalRationale.textContent = data.rationale;
        modalEvidence.textContent = data.evidence;
        modalConfidence.textContent = data.confidence;

        modalFactors.innerHTML = '';
        data.factors.forEach(factor => {
          const li = document.createElement('li');
          li.style.padding = '4px 0';
          li.style.color = '#F8FAFC';
          li.innerHTML = `<i class="fas fa-circle-check" style="color: #00FFB2; margin-right: 8px;"></i> ${factor}`;
          modalFactors.appendChild(li);
        });

        modalOverlay.classList.add('active');
      }
    });
  });

  if (modalClose) {
    modalClose.addEventListener('click', () => {
      modalOverlay.classList.remove('active');
    });
  }

  if (modalOverlay) {
    modalOverlay.addEventListener('click', (e) => {
      if (e.target === modalOverlay) {
        modalOverlay.classList.remove('active');
      }
    });
  }

  // --- 4. Executive PDF Report Download Simulation ---
  const exportBtn = document.getElementById('export-pdf-btn');
  const pdfModalOverlay = document.getElementById('pdf-modal-overlay');
  const pdfCloseBtn = document.getElementById('pdf-close-btn');

  if (exportBtn) {
    exportBtn.addEventListener('click', () => {
      if (pdfModalOverlay) {
        pdfModalOverlay.classList.add('active');
      }
    });
  }

  if (pdfCloseBtn) {
    pdfCloseBtn.addEventListener('click', () => {
      pdfModalOverlay.classList.remove('active');
    });
  }

  if (pdfModalOverlay) {
    pdfModalOverlay.addEventListener('click', (e) => {
      if (e.target === pdfModalOverlay) {
        pdfModalOverlay.classList.remove('active');
      }
    });
  }

  // --- 5. Interactive Dossier Live Search Input Simulator ---
  const searchInput = document.getElementById('demo-search-input');
  const profileName = document.getElementById('dossier-profile-name');
  const profileRole = document.getElementById('dossier-profile-role');

  if (searchInput) {
    searchInput.addEventListener('input', (e) => {
      const val = e.target.value.trim();
      if (val.length > 2) {
        const parts = val.split(' at ');
        if (parts.length > 1) {
          profileName.textContent = parts[0];
          profileRole.textContent = `Executive @ ${parts[1]}`;
        } else {
          profileName.textContent = val;
          profileRole.textContent = 'Searched Contact Intelligence Profile';
        }
      }
    });
  }

});
