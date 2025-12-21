# SOC 2 Compliance Guide

## Overview

SOC 2 (Service Organization Control 2) is a framework developed by the American Institute of CPAs (AICPA) for auditing and evaluating service organizations. It ensures that service providers (like cloud platforms, SaaS companies, etc.) have adequate controls and security measures in place to protect customer data and systems.

## What is SOC 2?

SOC 2 is an audited compliance framework that validates whether a service organization has the necessary controls to safeguard customer information and the integrity of systems. Unlike SOC 1 (which focuses on financial reporting), SOC 2 focuses on operational and security controls that affect service delivery.

### Key Point

SOC 2 is not a certification you earn once—it's an ongoing commitment to maintaining security controls verified through annual independent audits.

---

## The Five Trust Service Principles

SOC 2 is built on five core pillars called **Trust Service Criteria (TSC)**:

### 1. **Security (CC)**

**Principle**: The organization protects confidentiality and integrity of information.

**Key Areas**:

- Access controls and authentication mechanisms
- Encryption of data in transit and at rest
- Network segmentation and firewalls
- Vulnerability management and patch management
- Intrusion detection and monitoring
- Data classification and handling procedures

**What it means**: Your systems must have controls to prevent unauthorized access and protect data from being compromised.

---
---

### 2. **Availability (A)**

**Principle**: The organization ensures that systems are available for operation and use as committed.

**Key Areas**:

- System uptime and redundancy
- Disaster recovery plans (DRP)
- Business continuity procedures
- Backup and recovery testing
- Capacity planning and monitoring
- Infrastructure failover mechanisms

**What it means**: You must ensure services are operational as promised and can recover quickly from failures.

---

### 3. **Processing Integrity (PI)**

**Principle**: The organization ensures that information is complete, accurate, timely, and authorized.

**Key Areas**:

- Data validation and verification processes
- Error detection and correction procedures
- Complete and accurate transaction recording
- Authorization workflows and approvals
- System logs and audit trails
- Data quality monitoring

**What it means**: All processing of customer data must be accurate, complete, and properly authorized—no data corruption or unauthorized modifications.

---

### 4. **Confidentiality (C)**

**Principle**: The organization protects information designated as confidential.

**Key Areas**:

- Access controls limiting who can view sensitive data
- Encryption for sensitive information
- Classification and labeling of confidential data
- Secure data disposal procedures
- Non-disclosure agreements (NDAs) with staff
- Monitoring for unauthorized disclosure

**What it means**: Customer data and proprietary information must be protected and only accessible to authorized personnel.

---

### 5. **Privacy (P)**

**Principle**: The organization collects, uses, retains, discloses, and disposes of personal information with the purpose, consent, and proper controls.

**Key Areas**:

- Privacy policies that are clear and accessible
- Consent mechanisms for data collection
- Data retention policies aligned with regulations
- Customer rights (access, deletion, data portability)
- Compliance with data protection regulations (GDPR, CCPA, etc.)
- Privacy breach notification procedures

**What it means**: You must respect customer privacy, collect data only with consent, and comply with privacy regulations.

---

## SOC 2 Type I vs Type II

### **Type I**

- **Scope**: Point-in-time assessment
- **Duration**: Moment in time snapshot
- **Audit Length**: Single audit event
- **Purpose**: Validates controls are designed appropriately
- **Timeline**: Typically one quarter or less

### **Type II**

- **Scope**: Period-based assessment
- **Duration**: Minimum 6 months of operation (typically 12 months)
- **Audit Length**: Continuous monitoring and testing
- **Purpose**: Validates controls are designed AND operating effectively
- **Timeline**: Typically full year of monitoring

**Recommendation**: Type II is more valuable to customers and demonstrates sustained compliance.

---

## Core Control Categories

### A. Common Control Activities

#### 1. Control Environment

- Tone at the top (leadership commitment to security)
- Code of conduct and ethics policies
- Organizational structure and accountability
- Competence and training requirements

#### 2. Information and Communication

- Security policies and procedures documentation
- Employee training on security practices
- Incident reporting mechanisms
- Clear communication of security responsibilities

#### 3. Monitoring Activities

- Regular security assessments and testing
- Vulnerability scanning and penetration testing
- Log monitoring and analysis
- Control effectiveness reviews

---

### B. Entity-Level Controls

Controls that apply across the entire organization:

- Security governance and oversight
- Risk management processes

- **Incident Response Plan**: Steps to take if a security incident occurs
- **Business Continuity/Disaster Recovery Plan**: Recovery procedures
- **Change Management Process**: Controls for system changes
- **Vendor Management Policy**: How third-party vendors are evaluated
- **Employee Training**: Security awareness and onboarding procedures

### Operational Requirements

1. **Regular Audits & Testing**: Internal and external assessments
2. **Patch Management**: Timely application of security updates
3. **Vulnerability Management**: Regular scanning and remediation
4. **Monitoring & Logging**: Comprehensive audit trails
5. **Access Reviews**: Periodic verification of user access
6. **Backup & Recovery Testing**: Regular drills to verify DR procedures
7. **Penetration Testing**: Annual external testing
8. **Incident Tracking**: Documentation of all incidents

---

## Why SOC 2 Matters

### For Your Organization

- **Competitive Advantage**: Demonstrates trustworthiness to enterprise customers
- **Risk Reduction**: Identifies and mitigates security gaps
- **Operational Excellence**: Forces disciplined security practices
- **Regulatory Readiness**: Helps with compliance to other regulations (HIPAA, GDPR, etc.)

### For Your Customers

- **Assurance**: Verification that their data is protected
- **Due Diligence**: Fulfills their security requirements for vendor selection
- **Risk Mitigation**: Reduces their compliance burden
- **Trust**: Demonstrates commitment to security

---

## Common Control Gaps

Organizations often struggle with:

1. **Inadequate Documentation**: Policies exist but aren't formalized
2. **Weak Access Controls**: No MFA, excessive permissions, outdated access lists
3. **Poor Change Management**: Changes deployed without proper approval/testing
4. **Insufficient Monitoring**: Lack of audit logs and security monitoring
5. **Vendor Management**: Third-party risks not properly assessed
6. **Incident Response**: No formal procedures for security incidents
7. **Training & Awareness**: Staff not trained on security practices
8. **DR/BC Testing**: Procedures documented but not regularly tested

---

## Implementation Roadmap

### Phase 1: Assessment (Weeks 1-4)

- Evaluate current state against SOC 2 criteria
- Identify gaps and risks

- Document existing controls

---

## Infrastructure audit blockers (tracking)

For the infrastructure-side SOC 2 blockers (e.g., **disable public access + Private Endpoints** for Key Vault/Storage/ACR/Postgres/Redis), we track implementation **module-by-module** here:

- `docs/Development Documentation/private-access-implementation-tracker.md`

Current status (Terraform):

- **VNet module**: Implemented support for a dedicated private-endpoints subnet.
- **Key Vault module**: Implemented optional Private Endpoint + forces public access off when enabled (rollout is gated behind an environment toggle to avoid breaking existing deployments).

### Phase 2: Documentation (Weeks 5-8)

- Create/update security policies
- Document procedures and controls
- Define roles and responsibilities

### Phase 3: Implementation (Weeks 9-16)

- Deploy technical controls (MFA, encryption, logging)
- Establish monitoring and alerting
- Train staff on new procedures

### Phase 4: Testing & Optimization (Weeks 17-20)

- Internal audits and assessments
- Vulnerability testing
- Remediate identified issues

### Phase 5: Audit Preparation (Weeks 21-24)

- Gather audit evidence
- Prepare documentation for auditors
- Conduct final reviews
- Engage external auditors

---

## Ongoing Compliance Maintenance

### Monthly

- Review access logs
- Monitor security alerts
- Update vulnerability scans
- Employee security training (rotating topics)

### Quarterly

- Management review of security metrics
- Vendor risk assessment updates
- Policy and procedure reviews
- Incident trend analysis

### Annually

- External penetration testing
- Disaster recovery testing and drill
- Security awareness training
- SOC 2 audit/re-certification

### Continuous

- Patch management and updates
- Vulnerability remediation
- Incident response and investigation
- Access review and updates

---

## Common Questions

### Q: Is SOC 2 mandatory?

**A**: Not legally mandatory, but increasingly required by enterprise customers and business partners.

### Q: How much does SOC 2 compliance cost?

**A**: Varies widely ($50K-$500K+) depending on organization size, complexity, and current maturity.

### Q: How long does SOC 2 take to achieve?

**A**: 6-12 months for organizations starting from scratch; faster if controls already exist.

### Q: Do we need all five trust service criteria?

**A**: Organizations typically pursue Security (CC) and Availability (A) + one or more of the others based on their business model. Privacy (P) is increasingly required.

### Q: What happens if we fail the audit?

**A**: Auditors identify findings. Organizations must remediate and potentially face a re-audit or delayed certification.

---

## References

- [AICPA SOC 2 Trust Service Criteria](https://www.aicpa.org/interestareas/informationsystems/resources/soac-trustservicesprinciples)
- [SOC 2 Implementation Best Practices](https://www.aicpa.org/interestareas/informationsystems)
- Related Frameworks: ISO 27001, NIST Cybersecurity Framework, CIS Controls

---

**Last Updated**: December 17, 2025
